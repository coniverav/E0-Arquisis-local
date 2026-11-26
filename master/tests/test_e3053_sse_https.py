"""Local HTTPS route with real Nginx, curl, Uvicorn and the disposable test DB."""
import shutil
import socket
import subprocess
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
import uvicorn

from app.config import CORS_ALLOWED_ORIGINS
from app.main import app
from tests.test_e3032_e3033_sse import _commit_inbound, bearer  # shared signed test JWT


def wait_for(check, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    pytest.fail("Timed out waiting for the local SSE route")


@pytest.fixture
def https_route(tmp_path):
    for binary in ("nginx", "openssl", "curl"):
        if not shutil.which(binary):
            pytest.skip(f"Local HTTPS smoke test requires {binary}")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        "-out", str(cert), "-keyout", str(key),
    ], check=True, capture_output=True, timeout=10)
    with ExitStack() as cleanup:
        upstream = cleanup.enter_context(socket.socket())
        upstream.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [upstream]}, daemon=True)
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(setattr, server, "should_exit", True)
        wait_for(lambda: server.started)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        template = Path(__file__).resolve().parents[2] / "nginx/sse.conf.template"
        host = (template.read_text().replace("${SSE_HOST}", "localhost")
                .replace("${SSE_CERT}", str(cert)).replace("${SSE_KEY}", str(key))
                .replace("listen 443 ssl;", f"listen 127.0.0.1:{port} ssl;")
                .replace("/var/log/nginx/sse-access.log", str(tmp_path / "access.log")))
        conf = tmp_path / "nginx.conf"
        conf.write_text(
            "daemon off; master_process off; pid nginx.pid; error_log stderr; events {} http {\n"
            "client_body_temp_path body; proxy_temp_path proxy; fastcgi_temp_path fastcgi; "
            "scgi_temp_path scgi; uwsgi_temp_path uwsgi;\n"
            f"upstream energyshark_backend {{ server 127.0.0.1:{upstream.getsockname()[1]}; }}\n"
            + host + "\n}"
        )
        proxy = subprocess.Popen(["nginx", "-p", str(tmp_path), "-c", str(conf)])
        cleanup.callback(proxy.wait, 5)
        cleanup.callback(proxy.terminate)

        def ready():
            assert proxy.poll() is None
            with socket.socket() as probe:
                return probe.connect_ex(("127.0.0.1", port)) == 0

        wait_for(ready)
        yield ["curl", "--silent", "--show-error", "--noproxy", "*", "--cacert", str(cert),
               "--max-time", "30", f"https://localhost:{port}/events/stream"]


def test_progressive_authorized_https(https_route, bearer, tmp_path):
    origin = CORS_ALLOWED_ORIGINS[0]

    def response(*args, url=None):
        command = [*https_route[:-1], "-i", *args, url or https_route[-1]]
        return subprocess.check_output(command, text=True, timeout=8)

    assert " 401 " in response().splitlines()[0]
    assert " 400 " in response(url=https_route[-1] + "?cursor=0").splitlines()[0]
    assert " 404 " in response(url=https_route[-1].replace("/events/stream", "/internal/")).splitlines()[0]
    assert " 405 " in response("-X", "POST").splitlines()[0]
    forbidden = bearer("operator", scope="read:other")
    assert " 403 " in response("-H", f"Authorization: Bearer {forbidden}").splitlines()[0]
    preflight = ["-X", "OPTIONS", "-H", "Access-Control-Request-Method: GET",
                 "-H", "Access-Control-Request-Headers: Authorization,Last-Event-ID"]
    allowed = response(*preflight, "-H", f"Origin: {origin}").lower()
    assert f"access-control-allow-origin: {origin.lower()}" in allowed
    assert "authorization,last-event-id" in allowed
    denied = response(*preflight, "-H", "Origin: https://untrusted.invalid").lower()
    assert " 400 " in denied.splitlines()[0] and "access-control-allow-origin:" not in denied

    wire = tmp_path / "frames"
    token = bearer("operator")
    with wire.open("wb") as output:
        stream = subprocess.Popen([*https_route, "-iN", "-H", f"Origin: {origin}",
                                   "-H", f"Authorization: Bearer {token}"], stdout=output)
        try:
            _commit_inbound("offer")
            wait_for(lambda: "id: 1\n" in wire.read_text())
            assert stream.poll() is None
            _commit_inbound("bid")
            wait_for(lambda: "id: 2\n" in wire.read_text())
            wait_for(lambda: ": heartbeat\n" in wire.read_text(), seconds=20)
            assert stream.poll() is None
        finally:
            stream.terminate()
            stream.wait(5)
    payload = wire.read_text().lower()
    assert " 200 " in payload.splitlines()[0]
    assert "content-type: text/event-stream" in payload
    assert "cache-control: no-cache, no-store, no-transform" in payload
    assert f"access-control-allow-origin: {origin.lower()}" in payload
    assert "sentinel" not in payload

    resumed = response("-N", "-H", "Last-Event-ID: 1", "-H",
                       f"Authorization: Bearer {bearer('operator', expires_in=4)}")
    assert " 200 " in resumed.splitlines()[0]
    assert [line for line in resumed.splitlines() if line.startswith("id:")] == ["id: 2"]
