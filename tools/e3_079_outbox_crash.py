#!/usr/bin/env python3
"""E3-079: caída post-commit del outbox y recuperación del relay.

Levanta un entorno aislado (PostgreSQL + RabbitMQ locales, sin broker público ni
credenciales reales) y demuestra tres cosas sobre el camino real del relay:

1. Crash post-commit: el productor confirma la fila y no existe relay. Al
   arrancar el relay, publica el envelope guardado sin cambios.
2. Broker cortado: con RabbitMQ detenido el relay no pierde la intención; al
   volver el broker publica el mismo mensaje y el backlog queda en cero.
3. Lease vencido: un relay muere después de reclamar. Otro relay espera el
   lease, republica la misma intención y confirma una sola entrega.

Uso:
    python3 tools/e3_079_outbox_crash.py
    python3 tools/e3_079_outbox_crash.py --keep     # deja el entorno arriba

Requiere Docker con Compose v2. No toca AWS ni el despliegue real.
"""

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = "docker-compose.e3-079.yml"
COMPOSE = ["docker", "compose", "-f", COMPOSE_FILE]
MANAGEMENT = "http://127.0.0.1:15679/api"
CAPTURE_QUEUE = "e3079.capture"
DATABASE = "e3079"
USER = "e3079"
AUTH = base64.b64encode(f"{USER}:{USER}".encode()).decode()

FAILURES: list[str] = []
CAPTURED: list[dict] = []
TIMES: dict[str, float] = {}


def compose(*args: str, check: bool = True, stdin: str | None = None) -> str:
    result = subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker compose {' '.join(args)} falló:\n{result.stderr.strip()}"
        )
    return result.stdout


def sql(query: str) -> str:
    return compose(
        "exec", "-T", "db", "psql", "-U", USER, "-d", DATABASE, "-tA", "-c", query
    ).strip()


def row_state(row_id: int) -> dict:
    raw = sql(
        "SELECT row_to_json(t) FROM (SELECT id, status, attempt_count, "
        "dispatch_required, msg_id, idpk, payload, published_at, last_error, "
        "claim_token, lease_until FROM outbound_messages WHERE id = "
        f"{row_id}) t;"
    )
    return json.loads(raw)


def backlog() -> int:
    return int(
        sql(
            "SELECT count(*) FROM outbound_messages "
            "WHERE dispatch_required AND status <> 'PUBLISHED';"
        )
        or 0
    )


def management(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{MANAGEMENT}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"RabbitMQ {method} {path} -> {error.code}: {error.read().decode()[:300]}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"RabbitMQ inaccesible: {error.reason}") from error


def create_broker_identity() -> None:
    """Crea el usuario AMQP de la ciudad, igual que el broker real."""
    management(
        "PUT",
        "/users/city.KLD",
        {"password": "city.KLD", "tags": ""},
    )
    management(
        "PUT",
        "/permissions/%2F/city.KLD",
        {"configure": ".*", "write": ".*", "read": ".*"},
    )


def declare_capture_topology() -> None:
    management("PUT", "/exchanges/%2F/energy.x", {"type": "direct", "durable": True})
    management("PUT", "/exchanges/%2F/market", {"type": "topic", "durable": True})
    management("PUT", f"/queues/%2F/{CAPTURE_QUEUE}", {"durable": True})
    management(
        "POST",
        f"/bindings/%2F/e/energy.x/q/{CAPTURE_QUEUE}",
        {"routing_key": "city.KLD"},
    )
    management(
        "POST",
        f"/bindings/%2F/e/market/q/{CAPTURE_QUEUE}",
        {"routing_key": "market.#"},
    )


def drain_capture() -> list[dict]:
    """Consume la cola de captura y acumula lo recibido en la corrida."""
    response = management(
        "POST",
        f"/queues/%2F/{CAPTURE_QUEUE}/get",
        {"count": 50, "ackmode": "ack_requeue_false", "encoding": "auto"},
    )
    CAPTURED.extend(json.loads(item["payload"]) for item in response)
    return CAPTURED


def captured_count() -> int:
    return len(drain_capture())


def wait_for(description: str, predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    check(description, False, f"no se cumplió en {timeout:.0f}s")
    return False


def timed(label: str, started: float) -> None:
    """Registra el tiempo de recuperación observado (objetivo AD8: <= 60 s)."""
    elapsed = time.monotonic() - started
    TIMES[label] = elapsed
    print(f"     recuperación {label}: {elapsed:.1f}s (objetivo AD8 <= 60s)")


def check(description: str, condition: bool, detail: str = "") -> bool:
    status = "OK  " if condition else "FALLA"
    suffix = f" ({detail})" if detail else ""
    print(f"{status} {description}{suffix}")
    if not condition:
        FAILURES.append(description)
    return condition


def offer_envelope(cycle_id: str) -> dict:
    return {
        "msgId": str(uuid4()),
        "idpk": str(uuid4()),
        "type": "offer",
        "cycleId": cycle_id,
        "cityId": "KLD",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data": {
            "offerId": f"KLD-{cycle_id}",
            "quantity": 25,
            "pricePerEnergy": 120,
        },
    }


SEED_SCRIPT = """
import json
from datetime import datetime, timezone
from sqlmodel import Session

from app.database import engine
from app.services.outbox_store import enqueue_broker

envelope = json.loads({envelope!r})
with Session(engine) as session:
    row = enqueue_broker(
        session,
        envelope=envelope,
        exchange_name="market",
        routing_key="market.offer.KLD",
        now=datetime.now(timezone.utc),
    )
    row_id = row.id
    session.commit()
print(json.dumps({{"id": row_id, "envelope": envelope}}))
"""


def seed_offer(cycle_id: str) -> tuple[int, dict]:
    """Inserta negocio + outbox en una transacción y confirma (commit)."""
    envelope = offer_envelope(cycle_id)
    output = compose(
        "exec", "-T", "master", "python", "-",
        stdin=SEED_SCRIPT.format(envelope=json.dumps(envelope)),
    )
    payload = json.loads(output.strip().splitlines()[-1])
    return payload["id"], payload["envelope"]


def start_stack() -> None:
    print("== Levantando PostgreSQL, RabbitMQ y master aislados")
    compose("up", "-d", "--build", "db", "rabbitmq", "migrate", "master")
    compose("up", "-d", "--wait", "master")  # espera healthchecks de db/migrate
    create_broker_identity()
    declare_capture_topology()


def stop_relay() -> None:
    compose("kill", "relay", check=False)
    compose("rm", "-f", "relay", check=False)


def start_relay() -> None:
    # --no-deps: el driver controla el ciclo de vida. Sin esto, Compose
    # levantaría RabbitMQ de nuevo por el depends_on del escenario 2.
    compose("up", "-d", "--no-deps", "relay")


def wait_broker_down(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            management("GET", "/overview")
        except RuntimeError:
            return True
        time.sleep(0.5)
    return False


def scenario_crash_after_commit() -> None:
    print("\n== Escenario 1: crash tras commit, sin relay vivo")
    row_id, envelope = seed_offer(f"e3079-crash-{uuid4().hex[:8]}")
    state = row_state(row_id)
    check("la fila quedó confirmada en estado PENDING", state["status"] == "PENDING", state["status"])
    check("ningún intento de publicación ocurrió todavía", state["attempt_count"] == 0)
    check("la intención sigue marcada para despacho", state["dispatch_required"] is True)
    check("RabbitMQ no recibió nada antes del relay", captured_count() == 0)
    check("backlog inicial en 1", backlog() == 1, str(backlog()))

    started = time.monotonic()
    start_relay()
    wait_for(
        "el relay publica la fila recuperada",
        lambda: row_state(row_id)["status"] == "PUBLISHED",
    )
    timed("crash post-commit", started)
    state = row_state(row_id)
    check("el envelope publicado conserva msgId", state["msg_id"] == envelope["msgId"])
    check("el envelope publicado conserva idpk", state["idpk"] == envelope["idpk"])
    check("el payload publicado es idéntico al guardado", state["payload"] == envelope)

    wait_for("RabbitMQ recibe el mensaje", lambda: captured_count() == 1)
    received = CAPTURED[0]
    check("se entregó exactamente un mensaje", captured_count() == 1, str(len(CAPTURED)))
    check("el mensaje recibido tiene el mismo msgId", received["msgId"] == envelope["msgId"])
    check("el mensaje recibido tiene el mismo idpk", received["idpk"] == envelope["idpk"])
    check("el backlog vuelve a cero", backlog() == 0, str(backlog()))


def scenario_broker_down() -> None:
    print("\n== Escenario 2: broker cortado y recuperación")
    stop_relay()
    row_id, envelope = seed_offer(f"e3079-broker-{uuid4().hex[:8]}")
    compose("stop", "rabbitmq")
    check("el broker quedó realmente inaccesible", wait_broker_down())
    start_relay()
    time.sleep(6)
    state = row_state(row_id)
    check("con RabbitMQ detenido la fila sigue PENDING", state["status"] == "PENDING", state["status"])
    rows = int(sql("SELECT count(*) FROM outbound_messages;") or 0)
    check("el relay no inventó filas nuevas", rows == 2, f"filas={rows}")
    check("backlog conserva la intención", backlog() == 1, str(backlog()))

    started = time.monotonic()
    compose("up", "-d", "rabbitmq")
    wait_for(
        "el relay publica cuando el broker vuelve",
        lambda: row_state(row_id)["status"] == "PUBLISHED",
        timeout=90,
    )
    timed("broker recuperado", started)
    state = row_state(row_id)
    check("el payload republicado es idéntico", state["payload"] == envelope)
    check("msgId sin cambios tras la reconexión", state["msg_id"] == envelope["msgId"])
    wait_for("RabbitMQ recibe el mensaje pendiente", lambda: captured_count() == 2)
    check("el backlog vuelve a cero", backlog() == 0, str(backlog()))


def scenario_stale_lease() -> None:
    print("\n== Escenario 3: relay muerto con lease vigente")
    stop_relay()
    row_id, envelope = seed_offer(f"e3079-lease-{uuid4().hex[:8]}")
    sql(
        "UPDATE outbound_messages SET status = 'CLAIMED', "
        "claim_token = 'relay-muerto', lease_owner = 'relay-muerto', "
        "lease_until = now() + interval '5 seconds', "
        "attempt_count = attempt_count + 1 "
        f"WHERE id = {row_id};"
    )
    check("la fila quedó reclamada por un relay que murió", row_state(row_id)["status"] == "CLAIMED")
    start_relay()
    time.sleep(2)
    check("mientras el lease vive no se republica", captured_count() == 2)
    started = time.monotonic()
    wait_for(
        "al vencer el lease otro relay republica la intención",
        lambda: row_state(row_id)["status"] == "PUBLISHED",
        timeout=90,
    )
    timed("lease vencido", started)
    state = row_state(row_id)
    check("el segundo intento conserva el mismo payload", state["payload"] == envelope)
    check("el intento quedó registrado", state["attempt_count"] >= 2, str(state["attempt_count"]))
    wait_for("RabbitMQ recibe una sola copia", lambda: captured_count() == 3)
    check("el backlog vuelve a cero", backlog() == 0, str(backlog()))


def relay_logs() -> str:
    return compose("logs", "--no-log-prefix", "--tail", "30", "relay", check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="no bajar el entorno al terminar (para inspección manual)",
    )
    args = parser.parse_args()

    try:
        start_stack()
        scenario_crash_after_commit()
        scenario_broker_down()
        scenario_stale_lease()
    except Exception as error:  # noqa: BLE001 - el script reporta y deja evidencia
        FAILURES.append(str(error))
        print(f"\nERROR: {error}")
        print(relay_logs())
    finally:
        if not args.keep:
            print("\n== Bajando el entorno aislado")
            compose("down", "-v", "--remove-orphans", check=False)

    print()
    for label, elapsed in TIMES.items():
        print(f"TIEMPO {label}: {elapsed:.1f}s")
    if FAILURES:
        print(f"RESULTADO: {len(FAILURES)} fallo(s)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("RESULTADO: caída post-commit recuperada sin pérdidas; backlog en cero")
    return 0


if __name__ == "__main__":
    sys.exit(main())
