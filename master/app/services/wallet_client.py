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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Optional
import httpx
from sqlmodel import Session

from app.config import (
    CITY_ID,
    WALLET_API_KEY,
    WALLET_BASE_URL,
    WALLET_TIMEOUT_SECONDS,
)
from app.services.wallet_observability import ObservedWalletAsyncClient
from app.database import engine
from app.services.operational_rate_limits import block, reserve

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
    # `GET /balance` v3.0.1: obligatorios en el schema aunque sean null al
    # primer cierre. Se conservan para la conciliación de E2-044/E2-050.
    last_settled_cycle: Optional[str] = None
    balance_at_last_close_cr: Optional[Decimal] = None


@dataclass
class WalletSwapWriteResponse:
    status_code: int
    data: dict[str, Any]
    idempotency_key: str
    accepted_for_processing: bool  # True si es HTTP 202


@dataclass
class WalletStatementPage:
    entries: list[dict[str, Any]]
    next_cursor: Optional[str] = None


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

    def _reserve(self, category: str) -> None:
        with Session(engine) as session:
            retry_after = reserve(session, category)
            session.commit()
        if retry_after:
            raise WalletRateLimitError(
                "Wallet local rate limit or Retry-After active",
                status_code=429, retry_after=retry_after,
            )

    def _handle_response_error(self, response: httpx.Response, category: str | None = None) -> None:
        retry_after_str = response.headers.get("Retry-After")
        # OpenAPI v3.0.2 define segundos enteros (incluido cero), no HTTP-date.
        retry_after = int(retry_after_str) if retry_after_str and retry_after_str.isdecimal() else None
        if response.status_code in {429, 503} and category is not None and retry_after is not None:
            with Session(engine) as session:
                block(session, category, retry_after)
                session.commit()
        if response.status_code == 429:
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
                retry_after=retry_after if response.status_code == 503 else None,
            )


    async def get_balance(self) -> WalletBalanceResponse:
        """Consulta el saldo oficial de la propia ciudad (GET /balance)."""

        endpoint = f"{self.base_url}/balance"
        headers = self._get_headers()

        try:
            self._reserve("wallet:read")
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.get(endpoint, headers=headers)
                self._handle_response_error(res, "wallet:read")
                data = res.json()

                try:
                    assets = data["assets"]
                    kwh_info = assets["KWH"]
                    cr_info = assets["CR"]
                    if data["cityId"] != CITY_ID:
                        raise WalletError(
                            "GET /balance devolvió cityId inesperado: "
                            f"{data['cityId']!r} != {CITY_ID!r}"
                        )

                    last_close = data.get("balanceAtLastClose") or {}

                    return WalletBalanceResponse(
                        city_id=data["cityId"],
                        kwh_available=Decimal(str(kwh_info["available"])),
                        kwh_held=Decimal(str(kwh_info["held"])),
                        cr_available=Decimal(str(cr_info["available"])),
                        cr_held=Decimal(str(cr_info["held"])),
                        credit_line=Decimal(str(cr_info["creditLine"])),
                        timestamp=data.get("asOf"),
                        last_settled_cycle=data.get("lastSettledCycle"),
                        balance_at_last_close_cr=(
                            Decimal(str(last_close["CR"]))
                            if last_close.get("CR") is not None
                            else None
                        ),
                    )
                except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                    raise WalletError(
                        "Respuesta de GET /balance no cumple v3.0.1"
                    ) from exc

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
        expires_at: datetime | None = None,
    ) -> WalletSwapWriteResponse:
        """
        Propone un swap a la wallet (POST /swaps). Requiere Idempotency-Key.

        El contrato v3.0.1 es cerrado: no se envía `counterparty` y `memo`
        más `expiresAt` son obligatorios. La wallet elige al aceptante.
        """
        endpoint = f"{self.base_url}/swaps"
        headers = self._get_headers(idempotency_key=idempotency_key)
        # Wallet v3.0.1 propone swaps AL PORTADOR: NO acepta counterpart.
        # expiresAt forma parte del cuerpo idempotente; debe ser persistente.
        if expires_at is None or expires_at.tzinfo is None:
            raise ValueError("expires_at válido es obligatorio para POST /swaps")
        payload = {
            "give": {"asset": give_asset, "amount": float(give_amount)},
            "take": {"asset": take_asset, "amount": float(take_amount)},
            "expiresAt": expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "memo": memo,
        }

        try:
            self._reserve("wallet:write")
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.post(endpoint, json=payload, headers=headers)
                self._handle_response_error(res, "wallet:write")
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
            self._reserve("wallet:write")
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.post(endpoint, headers=headers)
                self._handle_response_error(res, "wallet:write")
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
            self._reserve("wallet:write")
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.post(endpoint, headers=headers)
                self._handle_response_error(res, "wallet:write")
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
            self._reserve("wallet:read")
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.get(endpoint, headers=headers)
                self._handle_response_error(res, "wallet:read")
                return res.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise WalletTimeoutError("GET /swaps failed") from exc

    async def get_statement(
        self,
        cycle_id: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> WalletStatementPage:
        """Consulta una página de `GET /statement` (`{entries, nextCursor}`)."""
        endpoint = f"{self.base_url}/statement"
        params: dict[str, str] = {}
        if cycle_id:
            params["cycleId"] = cycle_id
        if cursor:
            params["cursor"] = cursor
        headers = self._get_headers()

        try:
            self._reserve("wallet:read")
            async with ObservedWalletAsyncClient(timeout=self.timeout) as client:
                res = await client.get(endpoint, params=params, headers=headers)
                self._handle_response_error(res, "wallet:read")
                data = res.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise WalletTimeoutError("GET /statement failed") from exc

        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise WalletError("GET /statement no respetó el contrato {entries,nextCursor}")

        next_cursor = data.get("nextCursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise WalletError("GET /statement devolvió nextCursor inválido")

        return WalletStatementPage(
            entries=data["entries"],
            next_cursor=next_cursor,
        )
