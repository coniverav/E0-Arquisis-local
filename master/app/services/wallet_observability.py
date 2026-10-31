"""E2-027: métricas y logs seguros para llamadas REST a la wallet.

Solamente se registra metadata en lista blanca. Nunca se incluyen URL completas,
headers, payload, respuestas ni excepciones que puedan contener secretos.
"""

import logging
import os
import re
import time

import httpx

logger = logging.getLogger("energyshark.wallet.observability")

try:
    import newrelic.agent as nr_agent
except ImportError:  # Pruebas locales sin el agente instalado
    nr_agent = None


def register_wallet_worker_application() -> None:
    """Activa APM para el scheduler sin necesidad de peticiones HTTP."""
    if os.getenv("NEW_RELIC_MONITOR_MODE", "false").lower() not in ("true", "1", "yes"):
        return
    if nr_agent is None:
        logger.warning("New Relic habilitado, pero no está instalado el agente Python")
        return
    try:
        # Evita detener el scheduler esperando al recolector.
        nr_agent.register_application(timeout=0.0)
    except Exception:
        logger.warning("No fue posible registrar EnergyShark Scheduler en New Relic", exc_info=True)


def wallet_endpoint(url: str) -> str:
    """Reduce cualquier URL a una etiqueta de baja cardinalidad."""
    path = httpx.URL(url).path.rstrip("/")
    if path.endswith("/balance"):
        return "balance"
    if path.endswith("/statement"):
        return "statement"
    if path.endswith("/swaps"):
        return "swaps_propose"
    if re.search(r"/swaps/[^/]+/accept$", path):
        return "swaps_accept"
    if re.search(r"/swaps/[^/]+/cancel$", path):
        return "swaps_cancel"
    if re.search(r"/swaps/[^/]+$", path):
        return "swaps_get"
    return "other"


def _safe_idempotency_key(headers: object) -> str | None:
    if headers is None:
        return None
    try:
        value = httpx.Headers(headers).get("Idempotency-Key")
    except (TypeError, ValueError):
        return None
    # Un token o un valor inesperado nunca debe llegar a los logs.
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", value):
        return value
    return None


def record_wallet_request(*, method: str, endpoint: str, elapsed_ms: float,
                          outcome: str, status_code: int | None = None,
                          retry_after: str | None = None,
                          idempotency_key: str | None = None) -> None:
    attrs = {"method": method, "endpoint": endpoint, "outcome": outcome,
             "latencyMs": round(elapsed_ms, 2)}
    if status_code is not None:
        attrs["statusCode"] = status_code
    if retry_after is not None:
        # Header solo para uso operativo, acotado a un número en segundos.
        if retry_after.isdecimal():
            attrs["retryAfterSeconds"] = int(retry_after)
    if idempotency_key is not None:
        attrs["idpk"] = idempotency_key
    logger.info("wallet_http %s", " ".join(f"{k}={v}" for k, v in attrs.items()))
    if nr_agent is not None:
        try:
            nr_agent.record_custom_event("EnergySharkWalletHttp", attrs, application=nr_agent.application())
        except Exception:
            logger.debug("No fue posible enviar evento wallet a New Relic")


class ObservedWalletAsyncClient(httpx.AsyncClient):
    """Incorpora telemetría a GET y POST sin cambiar el contrato HTTP."""

    async def request(self, method: str, url: str, **kwargs):
        started = time.perf_counter()
        label = wallet_endpoint(str(url))
        idpk = _safe_idempotency_key(kwargs.get("headers"))
        try:
            response = await super().request(method, url, **kwargs)
        except httpx.TimeoutException:
            record_wallet_request(method=method, endpoint=label,
                                  elapsed_ms=(time.perf_counter() - started) * 1000,
                                  outcome="timeout", idempotency_key=idpk)
            raise
        except httpx.TransportError:
            record_wallet_request(method=method, endpoint=label,
                                  elapsed_ms=(time.perf_counter() - started) * 1000,
                                  outcome="transport_error", idempotency_key=idpk)
            raise
        record_wallet_request(
            method=method, endpoint=label,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            outcome=f"http_{response.status_code}",
            status_code=response.status_code,
            retry_after=response.headers.get("Retry-After") if response.status_code in {429, 503} else None,
            idempotency_key=idpk,
        )
        return response
