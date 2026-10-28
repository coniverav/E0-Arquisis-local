"""Verificación de swapToken ES256/JWKS (E2-040).

La wallet firma los claims de cada swap propuesto. El comprador debe verificar
la firma y comprobar que `sub`, `proposer`, `give`, `take`, `memo` y `exp`
coincidan con el bid que está aceptando. Este módulo no decide si el swap sigue
disponible: eso lo resuelve la wallet al aceptar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import time
from typing import Any, Callable, Optional

import httpx
import jwt
from jwt.algorithms import ECAlgorithm

from app.config import WALLET_ISSUER, WALLET_JWKS_URL

logger = logging.getLogger(__name__)

_JWKS_TTL_SECONDS = 600
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_REQUIRED_CLAIMS = (
    "iss",
    "sub",
    "iat",
    "exp",
    "proposer",
    "give",
    "take",
    "memo",
)

JwksFetcher = Callable[[str], dict[str, Any]]


class SwapTokenError(ValueError):
    """Token inválido o imposible de verificar."""

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def clear_jwks_cache() -> None:
    """Limpia la caché; útil en tests y ante rotación de llaves."""

    _jwks_cache.clear()


def _fetch_jwks(
    url: str,
    fetcher: Optional[JwksFetcher] = None,
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    current = time.monotonic() if now is None else now
    cached = _jwks_cache.get(url)
    if cached is not None and current - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]

    if fetcher is not None:
        payload = fetcher(url)
    else:
        try:
            response = httpx.get(url, timeout=10.0)
        except httpx.HTTPError as exc:
            raise SwapTokenError(
                "SWAP_TOKEN_JWKS_UNAVAILABLE", type(exc).__name__
            ) from exc
        if response.status_code != 200:
            raise SwapTokenError(
                "SWAP_TOKEN_JWKS_UNAVAILABLE",
                f"HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SwapTokenError("SWAP_TOKEN_JWKS_INVALID", "JSON inválido") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise SwapTokenError("SWAP_TOKEN_JWKS_INVALID", "falta keys[]")

    _jwks_cache[url] = (current, payload)
    return payload


def _key_for_kid(jwks: dict[str, Any], kid: str):
    for jwk in jwks.get("keys", []):
        if not isinstance(jwk, dict) or jwk.get("kid") != kid:
            continue
        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            raise SwapTokenError("SWAP_TOKEN_KEY_INVALID", "se esperaba EC P-256")
        try:
            return ECAlgorithm.from_jwk(json.dumps(jwk))
        except (ValueError, TypeError) as exc:
            raise SwapTokenError("SWAP_TOKEN_KEY_INVALID", str(exc)) from exc
    raise SwapTokenError("SWAP_TOKEN_UNKNOWN_KID", kid)


def _claim_amount(claims: dict[str, Any], name: str) -> tuple[str, Decimal]:
    value = claims.get(name)
    if not isinstance(value, dict):
        raise SwapTokenError(f"SWAP_TOKEN_{name.upper()}_INVALID", "no es objeto")

    asset = value.get("asset")
    if asset not in {"CR", "KWH"}:
        raise SwapTokenError(
            f"SWAP_TOKEN_{name.upper()}_INVALID", f"asset={asset!r}"
        )

    try:
        amount = Decimal(str(value.get("amount")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SwapTokenError(
            f"SWAP_TOKEN_{name.upper()}_INVALID", "amount no numérico"
        ) from exc

    if amount <= 0:
        raise SwapTokenError(
            f"SWAP_TOKEN_{name.upper()}_INVALID", "amount debe ser positivo"
        )

    return str(asset), amount


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def verify_swap_token(
    token: str,
    *,
    swap_id: str,
    proposer: str,
    give_asset: str,
    give_amount: Decimal,
    take_asset: str,
    take_amount: Decimal,
    memo: str,
    now: Optional[datetime] = None,
    jwks_url: Optional[str] = None,
    jwks_fetcher: Optional[JwksFetcher] = None,
) -> dict[str, Any]:
    """Verifica firma y claims; devuelve los claims si todo coincide.

    Lanza `SwapTokenError` con un `reason` estable para la auditoría.
    """

    if not isinstance(token, str) or not token.strip():
        raise SwapTokenError("SWAP_TOKEN_MISSING")

    url = jwks_url or WALLET_JWKS_URL
    if not url:
        raise SwapTokenError("SWAP_TOKEN_JWKS_NOT_CONFIGURED")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise SwapTokenError("SWAP_TOKEN_MALFORMED", str(exc)) from exc

    if header.get("alg") != "ES256":
        raise SwapTokenError(
            "SWAP_TOKEN_ALGORITHM", f"alg={header.get('alg')!r}"
        )

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise SwapTokenError("SWAP_TOKEN_UNKNOWN_KID", "falta kid")

    key = _key_for_kid(_fetch_jwks(url, jwks_fetcher), kid)

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["ES256"],
            issuer=WALLET_ISSUER,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SwapTokenError("SWAP_TOKEN_EXPIRED") from exc
    except jwt.InvalidIssuerError as exc:
        raise SwapTokenError("SWAP_TOKEN_ISSUER", str(exc)) from exc
    except jwt.PyJWTError as exc:
        raise SwapTokenError("SWAP_TOKEN_SIGNATURE", str(exc)) from exc

    current = _as_utc(now or datetime.now(timezone.utc))
    expires_at = claims.get("exp")
    if not isinstance(expires_at, int):
        raise SwapTokenError("SWAP_TOKEN_EXPIRED", "exp no entero")
    if datetime.fromtimestamp(expires_at, tz=timezone.utc) <= current:
        raise SwapTokenError("SWAP_TOKEN_EXPIRED")

    if claims.get("sub") != swap_id:
        raise SwapTokenError(
            "SWAP_TOKEN_SUB_MISMATCH",
            f"{claims.get('sub')!r} != {swap_id!r}",
        )

    if claims.get("proposer") != proposer:
        raise SwapTokenError(
            "SWAP_TOKEN_PROPOSER_MISMATCH",
            f"{claims.get('proposer')!r} != {proposer!r}",
        )

    token_give_asset, token_give_amount = _claim_amount(claims, "give")
    if token_give_asset != give_asset or token_give_amount != Decimal(str(give_amount)):
        raise SwapTokenError(
            "SWAP_TOKEN_GIVE_MISMATCH",
            f"{token_give_asset} {token_give_amount} != {give_asset} {give_amount}",
        )

    token_take_asset, token_take_amount = _claim_amount(claims, "take")
    if token_take_asset != take_asset or token_take_amount != Decimal(str(take_amount)):
        raise SwapTokenError(
            "SWAP_TOKEN_TAKE_MISMATCH",
            f"{token_take_asset} {token_take_amount} != {take_asset} {take_amount}",
        )

    if claims.get("memo") != memo:
        raise SwapTokenError(
            "SWAP_TOKEN_MEMO_MISMATCH",
            f"{claims.get('memo')!r} != {memo!r}",
        )

    return claims
