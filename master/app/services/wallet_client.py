"""Cliente REST autenticado para la Wallet Central v3 (E2-019, E2-020).

Implementa:
- Autenticación mediante header X-Api-Key.
- Manejo estricto de Idempotency-Key durable (UUID) para escrituras (POST).
- Manejo de respuestas HTTP 202 (Accepted) sin asumir liquidación prematura (AD5).
- Respeto a límites de velocidad y Retry-After (HTTP 429).
- Métodos tipados para /swaps, /swaps/{id}/accept, /swaps/{id}/cancel, /balance, /statement.
- Trazabilidad y logs que NUNCA exponen la clave X-Api-Key.
"""

from dataclasses import dataclass
from decimal import Decimal
import logging
from typing import Any, Optional
import httpx

from app.config import WALLET_API_KEY, WALLET_BASE_URL, WALLET_TIMEOUT_SECONDS
from app.services.wallet_observability import ObservedWalletAsyncClient

logger = logging.getLogger(__name__)


class WalletError(Exception):
    """Error base devuelto por la Wallet o en la comunicación con ella."""

    def __init__(self, message: str, status_code: Optional[int] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class WalletRateLimitError(WalletError):
    """Excepción para HTTP 429 Too Many Requests con Retry-After."""
    pass


class WalletTimeoutError(WalletError):
    """Excepción para timeouts o errores de conectividad de red con la wallet."""
    pass


@dataclass
class WalletBalanceResponse:
    city_id: str
    kwh_available: Decimal
    kwh_held: Decimal
    cr_available: Decimal
    cr_held: Decimal
    credit_line: Decimal
    timestamp: Optional[str] = None


@dataclass
class WalletSwapWriteResponse:
    status_code: int
    data: dict[str, Any]
    idempotency_key: str
    accepted_for_processing: bool  # True si es HTTP 202


class WalletClient:
    """Cliente HTTP tipado para interactuar con la Wallet Central."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = (base_url or WALLET_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else WALLET_API_KEY
        self.timeout = timeout if timeout is not None else WALLET_TIMEOUT_SECONDS

    def _get_headers(self, idempotency_key: Optional[str] = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _mask_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Enmascara la cabecera sensible para registro seguro de logs."""
        masked = dict(headers)
        if "X-Api-Key" in masked:
            masked["X-Api-Key"] = "***REDACTED***"
        return masked

    def _handle_response_error(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            retry_after_str = response.headers.get("Retry-After")
            retry_after = int(retry_after_str) if retry_after_str and retry_after_str.isdigit() else None
            logger.warning(
                "Wallet rate limit exceeded (HTTP 429). Retry-After: %s", retry_after
            )
            raise WalletRateLimitError(
                f"Rate limit exceeded (HTTP 429). Retry-After: {retry_after}",
                status_code=429,
                retry_after=retry_after,
            )
        if response.is_error:
            logger.error("Wallet HTTP error status_code=%s", response.status_code)
            raise WalletError(
                f"Wallet request failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )

    async def get_balance(self, city_id: Optional[str] = None) -> WalletBalanceResponse:
        """
        Consulta saldos disponibles y retenidos (GET /balance).
        """
        endpoint = f"{self.base_url}/balance"
        params = {"cityId": city_id} if city_id else {}
        headers = self._get_headers()

        try:
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.get(endpoint, params=params, headers=headers)
                self._handle_response_error(res)
                data = res.json()

                # Normalización de respuesta según catálogo
                # Estructura habitual: {"cityId": ..., "kwh": {"available": ..., "held": ...}, "cr": ...}
                kwh_info = data.get("kwh", {})
                cr_info = data.get("cr", {})
                return WalletBalanceResponse(
                    city_id=data.get("cityId", city_id or ""),
                    kwh_available=Decimal(str(kwh_info.get("available", 0))),
                    kwh_held=Decimal(str(kwh_info.get("held", 0))),
                    cr_available=Decimal(str(cr_info.get("available", 0))),
                    cr_held=Decimal(str(cr_info.get("held", 0))),
                    credit_line=Decimal(str(data.get("creditLine", 0))),
                    timestamp=data.get("timestamp"),
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.error("Wallet connection error endpoint=balance error_type=%s", type(exc).__name__)
            raise WalletTimeoutError("Wallet balance connection failed") from exc

    async def propose_swap(
        self,
        *,
        idempotency_key: str,
        give_asset: str,
        give_amount: Decimal,
        take_asset: str,
        take_amount: Decimal,
        counterpart_city_id: str,
        memo: Optional[str] = None,
    ) -> WalletSwapWriteResponse:
        """
        Propone un swap a la wallet (POST /swaps). Requiere Idempotency-Key.
        Típicamente retorna HTTP 202 Accepted.
        """
        endpoint = f"{self.base_url}/swaps"
        headers = self._get_headers(idempotency_key=idempotency_key)
        payload = {
            "give": {"asset": give_asset, "amount": float(give_amount)},
            "take": {"asset": take_asset, "amount": float(take_amount)},
            "counterpart": counterpart_city_id,
        }
        if memo:
            payload["memo"] = memo

        try:
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.post(endpoint, json=payload, headers=headers)
                self._handle_response_error(res)
                return WalletSwapWriteResponse(
                    status_code=res.status_code,
                    data=res.json() if res.content else {},
                    idempotency_key=idempotency_key,
                    accepted_for_processing=(res.status_code == 202),
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning("Wallet connection error endpoint=swaps_propose error_type=%s", type(exc).__name__)
            raise WalletTimeoutError("POST /swaps timed out or network failed") from exc

    async def accept_swap(
        self,
        swap_id: str,
        *,
        idempotency_key: str,
    ) -> WalletSwapWriteResponse:
        """
        Acepta un swap existente (POST /swaps/{swap_id}/accept).
        Requiere Idempotency-Key. Devuelve HTTP 202 Accepted.
        """
        endpoint = f"{self.base_url}/swaps/{swap_id}/accept"
        headers = self._get_headers(idempotency_key=idempotency_key)

        try:
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.post(endpoint, headers=headers)
                self._handle_response_error(res)
                return WalletSwapWriteResponse(
                    status_code=res.status_code,
                    data=res.json() if res.content else {},
                    idempotency_key=idempotency_key,
                    accepted_for_processing=(res.status_code == 202),
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning("Wallet connection error endpoint=swaps_accept error_type=%s", type(exc).__name__)
            raise WalletTimeoutError("POST /swaps accept timed out") from exc

    async def cancel_swap(
        self,
        swap_id: str,
        *,
        idempotency_key: str,
    ) -> WalletSwapWriteResponse:
        """
        Cancela un swap existente (POST /swaps/{swap_id}/cancel).
        """
        endpoint = f"{self.base_url}/swaps/{swap_id}/cancel"
        headers = self._get_headers(idempotency_key=idempotency_key)

        try:
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.post(endpoint, headers=headers)
                self._handle_response_error(res)
                return WalletSwapWriteResponse(
                    status_code=res.status_code,
                    data=res.json() if res.content else {},
                    idempotency_key=idempotency_key,
                    accepted_for_processing=(res.status_code == 202),
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise WalletTimeoutError("POST /swaps cancel timed out") from exc

    async def get_swap(self, swap_id: str) -> dict[str, Any]:
        """
        Consulta estado de un swap por su swapId (GET /swaps/{swap_id}).
        """
        endpoint = f"{self.base_url}/swaps/{swap_id}"
        headers = self._get_headers()

        try:
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.get(endpoint, headers=headers)
                self._handle_response_error(res)
                return res.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise WalletTimeoutError("GET /swaps failed") from exc

    async def get_statement(self, cycle_id: Optional[str] = None) -> list[dict[str, Any]]:
        """
        Consulta el estado de cuenta y movimientos (GET /statement).
        """
        endpoint = f"{self.base_url}/statement"
        params = {"cycleId": cycle_id} if cycle_id else {}
        headers = self._get_headers()

        try:
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.get(endpoint, params=params, headers=headers)
                self._handle_response_error(res)
                data = res.json()
                return data if isinstance(data, list) else data.get("transactions", [])
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise WalletTimeoutError("GET /statement failed") from exc
