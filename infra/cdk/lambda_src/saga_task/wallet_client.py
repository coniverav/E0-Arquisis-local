from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from errors import (
    WalletIdempotencyMismatch,
    WalletNotFound,
    WalletPermanent,
    WalletRateLimited,
    WalletRejected,
    WalletRetryable,
    WalletUnauthorized,
)


@dataclass(frozen=True)
class WalletConfig:
    base_url: str
    api_key: str
    timeout_seconds: float = 10.0
    max_attempts: int = 4

    @classmethod
    def from_secret(cls, secret: Mapping[str, Any]) -> "WalletConfig":
        base_url = secret.get("baseUrl") or secret.get("WALLET_BASE_URL")
        api_key = secret.get("apiKey") or secret.get("WALLET_API_KEY")
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValueError("El secret Wallet requiere baseUrl HTTPS")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("El secret Wallet requiere apiKey")
        config = cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout_seconds=float(secret.get("timeoutSeconds", 10.0)),
            max_attempts=int(secret.get("maxAttempts", 4)),
        )
        if config.timeout_seconds <= 0 or config.max_attempts <= 0:
            raise ValueError("Timeout e intentos Wallet deben ser positivos")
        return config


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    data: dict[str, Any]


@dataclass(frozen=True)
class WalletResult:
    status_code: int
    data: dict[str, Any]
    accepted_for_processing: bool
    idempotency_key: str | None


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class Quota(Protocol):
    def reserve(self, category: str) -> int: ...

    def block(self, category: str, seconds: int) -> None: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
                return HttpResponse(
                    response.status,
                    dict(response.headers.items()),
                    _json_object(payload),
                )
        except HTTPError as exc:
            return HttpResponse(
                exc.code,
                dict(exc.headers.items()) if exc.headers else {},
                _json_object(exc.read()),
            )


def _json_object(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WalletPermanent("Wallet devolvió JSON inválido") from exc
    if not isinstance(value, dict):
        raise WalletPermanent("Wallet devolvió un cuerpo no estructurado")
    return value


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = next(
        (header_value for name, header_value in headers.items()
         if name.lower() == "retry-after"),
        None,
    )
    if value is None or not str(value).isdigit():
        return None
    return int(value)


class WalletClient:
    """Cliente sin estado: todos los reintentos conservan la misma llave."""

    def __init__(
        self,
        config: WalletConfig,
        quota: Quota,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.quota = quota
        self.transport = transport or UrllibTransport()
        self.sleep = sleep
        self.monotonic = monotonic

    def propose(
        self,
        *,
        idempotency_key: str,
        swap: Mapping[str, Any],
        deadline_seconds: float,
    ) -> WalletResult:
        return self._request(
            "POST",
            "/swaps",
            idempotency_key=idempotency_key,
            payload=dict(swap),
            category="wallet:write",
            deadline_seconds=deadline_seconds,
        )

    def accept(
        self,
        *,
        swap_id: str,
        idempotency_key: str,
        deadline_seconds: float,
    ) -> WalletResult:
        return self._request(
            "POST",
            f"/swaps/{swap_id}/accept",
            idempotency_key=idempotency_key,
            payload=None,
            category="wallet:write",
            deadline_seconds=deadline_seconds,
        )

    def cancel(
        self,
        *,
        swap_id: str,
        idempotency_key: str,
        deadline_seconds: float,
    ) -> WalletResult:
        return self._request(
            "POST",
            f"/swaps/{swap_id}/cancel",
            idempotency_key=idempotency_key,
            payload=None,
            category="wallet:write",
            deadline_seconds=deadline_seconds,
        )

    def get_swap(self, *, swap_id: str, deadline_seconds: float) -> WalletResult:
        return self._request(
            "GET",
            f"/swaps/{swap_id}",
            idempotency_key=None,
            payload=None,
            category="wallet:read",
            deadline_seconds=deadline_seconds,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str | None,
        payload: dict[str, Any] | None,
        category: str,
        deadline_seconds: float,
    ) -> WalletResult:
        if method == "POST" and not idempotency_key:
            raise ValueError("Idempotency-Key estable es obligatoria")
        if deadline_seconds <= 0:
            raise WalletRetryable("No queda tiempo para consultar Wallet")

        deadline = self.monotonic() + deadline_seconds
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Api-Key": self.config.api_key,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None else None
        )

        last_error: WalletRetryable | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            quota_wait = self.quota.reserve(category)
            if quota_wait:
                self._wait_or_raise(quota_wait, deadline, "cuota compartida")
                quota_wait = self.quota.reserve(category)
                if quota_wait:
                    raise WalletRateLimited(
                        f"La cuota compartida exige esperar {quota_wait} segundos"
                    )
            try:
                response = self.transport.request(
                    method,
                    f"{self.config.base_url}{path}",
                    headers=headers,
                    body=body,
                    timeout=min(
                        self.config.timeout_seconds,
                        max(0.1, deadline - self.monotonic()),
                    ),
                )
            except (TimeoutError, socket.timeout, URLError, OSError) as exc:
                last_error = WalletRetryable("Fallo transitorio al invocar Wallet")
                if attempt == self.config.max_attempts:
                    raise last_error from exc
                self._wait_or_raise(2 ** (attempt - 1), deadline, "backoff")
                continue

            if response.status_code in {200, 202}:
                return WalletResult(
                    response.status_code,
                    response.data,
                    response.status_code == 202,
                    idempotency_key,
                )
            if response.status_code == 429:
                wait = _retry_after(response.headers)
                if wait is None:
                    raise WalletRateLimited("HTTP 429 sin Retry-After válido")
                self.quota.block(category, wait)
                last_error = WalletRateLimited(
                    f"Wallet exige esperar {wait} segundos"
                )
                if attempt == self.config.max_attempts:
                    raise last_error
                self._wait_or_raise(wait, deadline, "Retry-After")
                continue
            if 500 <= response.status_code < 600:
                retry_after = _retry_after(response.headers) or 0
                if retry_after:
                    self.quota.block(category, retry_after)
                last_error = WalletRetryable(
                    f"Wallet respondió HTTP {response.status_code}"
                )
                if attempt == self.config.max_attempts:
                    raise last_error
                self._wait_or_raise(
                    max(retry_after, 2 ** (attempt - 1)),
                    deadline,
                    "backoff",
                )
                continue
            self._raise_permanent(response)

        raise last_error or WalletRetryable("Wallet no entregó respuesta")

    def _wait_or_raise(self, seconds: int | float, deadline: float, reason: str) -> None:
        if self.monotonic() + seconds >= deadline:
            if reason in {"Retry-After", "cuota compartida"}:
                raise WalletRateLimited(
                    f"{reason} excede el deadline de la tarea"
                )
            raise WalletRetryable(f"{reason} excede el deadline de la tarea")
        self.sleep(seconds)

    @staticmethod
    def _raise_permanent(response: HttpResponse) -> None:
        code = response.data.get("code") or response.data.get("error")
        if response.status_code == 422 and code == "IDEMPOTENCY_MISMATCH":
            raise WalletIdempotencyMismatch("Wallet rechazó la llave idempotente")
        if response.status_code == 401:
            raise WalletUnauthorized("Wallet rechazó la autenticación")
        if response.status_code == 404:
            raise WalletNotFound("Wallet no encontró el recurso")
        if 400 <= response.status_code < 500:
            raise WalletRejected(f"Wallet rechazó la solicitud: HTTP {response.status_code}")
        raise WalletPermanent(f"Respuesta Wallet no soportada: HTTP {response.status_code}")
