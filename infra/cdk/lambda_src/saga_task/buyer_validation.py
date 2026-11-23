"""E3-045: verificar swapToken seller vía JWKS antes de habilitar aceptación.

Replica el contrato de E2 swap_tokens.verify_swap_token dentro de la imagen
Lambda (con build context aislado). No registra, devuelve ni publica el JWT.
La BD de E3-044 es la única fuente del token y de las identidades económicas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import jwt
from jwt.algorithms import ECAlgorithm


class BuyerInvalidToken(RuntimeError):
    """Firma, formato, algoritmo o llave no válidos."""


class BuyerExpiredToken(RuntimeError):
    """JWT expirado según el tiempo del sistema."""


class BuyerClaimsMismatch(RuntimeError):
    """Claims firmados o identidades persistidas no coinciden."""


class BuyerJwksUnavailable(RuntimeError):
    """No fue posible consultar de manera confiable las llaves públicas."""


class BuyerValidationInconsistent(RuntimeError):
    """La ejecución local y la operación económica no son compatibles."""


_JWKS_TTL = 600
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise BuyerValidationInconsistent(code)


def _utc(value: Any, code: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BuyerValidationInconsistent(code) from exc
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None, code)
    return value.astimezone(timezone.utc)


def _amount(value: Any, *, code: str) -> Decimal:
    if isinstance(value, bool):
        raise BuyerClaimsMismatch(code)
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise BuyerClaimsMismatch(code) from exc
    if not number.is_finite() or number <= 0:
        raise BuyerClaimsMismatch(code)
    return number


def jwks_settings(secret: dict[str, Any]) -> tuple[str, str]:
    """JWKS de la raíz del origen REST /v1, salvo URL oficial configurada."""
    base = secret.get("baseUrl") or secret.get("WALLET_BASE_URL")
    _require(isinstance(base, str), "BUYER_WALLET_BASE_URL_REQUIRED")
    parsed = urlsplit(base)
    _require(parsed.scheme == "https" and bool(parsed.netloc)
             and parsed.username is None and parsed.password is None,
             "BUYER_WALLET_BASE_URL_INVALID")
    selected = (secret.get("jwksUrl") or secret.get("WALLET_JWKS_URL")
                or f"{parsed.scheme}://{parsed.netloc}/.well-known/jwks.json")
    parsed_jwks = urlsplit(selected)
    _require(parsed_jwks.scheme == "https" and bool(parsed_jwks.netloc)
             and parsed_jwks.username is None and parsed_jwks.password is None
             and parsed_jwks.fragment == "",
             "BUYER_WALLET_JWKS_URL_INVALID")
    issuer = secret.get("issuer") or secret.get("WALLET_ISSUER") or "wallet.energyshark"
    _require(isinstance(issuer, str) and bool(issuer), "BUYER_WALLET_ISSUER_INVALID")
    return selected, issuer


def _remote_jwks(url: str) -> dict[str, Any]:
    try:
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=6) as response:
            if response.status != 200:
                raise BuyerJwksUnavailable("BUYER_JWKS_HTTP_UNAVAILABLE")
            raw = response.read(128 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise BuyerJwksUnavailable("BUYER_JWKS_UNAVAILABLE") from exc
    if len(raw) > 128 * 1024:
        raise BuyerJwksUnavailable("BUYER_JWKS_TOO_LARGE")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BuyerJwksUnavailable("BUYER_JWKS_INVALID_JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise BuyerJwksUnavailable("BUYER_JWKS_INVALID_DOCUMENT")
    return payload


def _jwks(url: str, fetch_jwks: Callable[[str], dict[str, Any]] | None) -> dict[str, Any]:
    if fetch_jwks is not None:
        try:
            keys = fetch_jwks(url)
        except Exception as exc:
            raise BuyerJwksUnavailable("BUYER_JWKS_UNAVAILABLE") from exc
    else:
        entry = _JWKS_CACHE.get(url)
        now = time.monotonic()
        if entry is not None and now - entry[0] < _JWKS_TTL:
            return entry[1]
        keys = _remote_jwks(url)
        _JWKS_CACHE[url] = (now, keys)
    if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
        raise BuyerJwksUnavailable("BUYER_JWKS_INVALID_DOCUMENT")
    return keys


def _verify_jwt(
    token: str, *, jwks_url: str, issuer: str, now: datetime,
    fetch_jwks: Callable[[str], dict[str, Any]] | None,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise BuyerInvalidToken("BUYER_TOKEN_INVALID")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise BuyerInvalidToken("BUYER_TOKEN_MALFORMED") from exc
    if header.get("alg") != "ES256":
        raise BuyerInvalidToken("BUYER_TOKEN_ALGORITHM")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise BuyerInvalidToken("BUYER_TOKEN_KID_MISSING")
    jwks = _jwks(jwks_url, fetch_jwks)
    key = None
    for item in jwks["keys"]:
        if isinstance(item, dict) and item.get("kid") == kid:
            if item.get("kty") != "EC" or item.get("crv") != "P-256":
                raise BuyerInvalidToken("BUYER_TOKEN_KEY_INVALID")
            try:
                key = ECAlgorithm.from_jwk(json.dumps(item))
            except (TypeError, ValueError) as exc:
                raise BuyerInvalidToken("BUYER_TOKEN_KEY_INVALID") from exc
            break
    if key is None:
        raise BuyerInvalidToken("BUYER_TOKEN_UNKNOWN_KID")
    try:
        claims = jwt.decode(
            token, key, algorithms=["ES256"], issuer=issuer,
            options={"require": ["iss", "sub", "iat", "exp", "proposer", "give", "take", "memo"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise BuyerExpiredToken("BUYER_TOKEN_EXPIRED") from exc
    except jwt.InvalidIssuerError as exc:
        raise BuyerClaimsMismatch("BUYER_TOKEN_ISSUER_MISMATCH") from exc
    except jwt.PyJWTError as exc:
        raise BuyerInvalidToken("BUYER_TOKEN_SIGNATURE_OR_CLAIMS_INVALID") from exc
    exp = claims.get("exp")
    if type(exp) is not int:
        raise BuyerInvalidToken("BUYER_TOKEN_EXP_INVALID")
    if datetime.fromtimestamp(exp, timezone.utc) <= now:
        raise BuyerExpiredToken("BUYER_TOKEN_EXPIRED")
    return claims


def validate_buyer_token(
    store: Any, *, saga_execution_id: str, swap_record_id: int,
    swap_id: str, expected: dict[str, Any], jwks_url: str, issuer: str,
    fetch_jwks: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sólo lecturas DB; nunca retorna JWT, claims completos ni secretos."""
    _require(isinstance(saga_execution_id, str) and bool(saga_execution_id)
             and type(swap_record_id) is int and swap_record_id > 0
             and isinstance(swap_id, str) and bool(swap_id)
             and isinstance(expected, dict), "BUYER_VALIDATION_IDENTITY_REQUIRED")
    now = _utc(store.now(), "BUYER_VALIDATION_CLOCK_INVALID")
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT se.role, se.status, se.cycle_id, se.swap_id,
                          sr.idpk, sr.role, sr.swap_id, sr.cycle_id, sr.status,
                          sr.counterpart_city_id, sr.give_asset, sr.give_amount,
                          sr.take_asset, sr.take_amount, sr.swap_token,
                          sr.expires_at, sr.accept_idempotency_key,
                          b.direction, b.status, b.cycle_id, b.counterpart_city_id,
                          b.swap_id, b.offer_id, b.quantity
                   FROM swap_records sr
                   JOIN saga_executions se ON se.idpk = sr.idpk
                   JOIN market_bids b ON b.idpk = sr.source_bid_idpk
                   WHERE sr.id = %s AND se.idpk = %s""",
                (swap_record_id, saga_execution_id),
            )
            row = cursor.fetchone()
    _require(row is not None, "BUYER_VALIDATION_ECONOMIC_ROWS_MISSING")
    (saga_role, saga_status, saga_cycle, saga_swap,
     record_idpk, role, record_swap, record_cycle, record_status,
     seller, give_asset, give_amount, take_asset, take_amount, token,
     expires_at, accept_key, bid_direction, bid_status, bid_cycle,
     bid_seller, bid_swap, offer_id, quantity) = row
    _require(saga_role == "BUYER" and saga_status in {"PENDING_START", "RUNNING", "WAITING"}
             and saga_cycle == record_cycle == bid_cycle
             and saga_swap == record_swap == bid_swap == swap_id
             and record_idpk == accept_key == saga_execution_id
             and role == "ACCEPTOR" and record_status == "SUBMITTED"
             and bid_direction == "OUTGOING" and bid_status == "ACCEPTED"
             and seller == bid_seller
             and give_asset == "CR" and take_asset == "KWH"
             and Decimal(str(take_amount)) == Decimal(str(quantity))
             and isinstance(token, str) and bool(token) and expires_at is not None,
             "BUYER_VALIDATION_ECONOMIC_IDENTITY_MISMATCH")
    claims_give = expected.get("give")
    claims_take = expected.get("take")
    if not isinstance(claims_give, dict) or not isinstance(claims_take, dict):
        raise BuyerClaimsMismatch("BUYER_EXPECTED_CLAIMS_INVALID")
    if (expected.get("proposer") != seller
            or expected.get("memo") != offer_id
            or claims_give.get("asset") != "KWH"
            or claims_take.get("asset") != "CR"
            or _amount(claims_give.get("amount"), code="BUYER_GIVE_AMOUNT_INVALID") != Decimal(str(quantity))
            or _amount(claims_take.get("amount"), code="BUYER_TAKE_AMOUNT_INVALID") != Decimal(str(give_amount))):
        raise BuyerClaimsMismatch("BUYER_EXPECTED_CLAIMS_MISMATCH")
    token_exp = _utc(expires_at, "BUYER_TOKEN_STORED_EXPIRY_INVALID")
    if token_exp <= now:
        raise BuyerExpiredToken("BUYER_TOKEN_EXPIRED")
    claims = _verify_jwt(token, jwks_url=jwks_url, issuer=issuer, now=now,
                         fetch_jwks=fetch_jwks)
    if (claims.get("sub") != swap_id or claims.get("proposer") != seller
            or claims.get("memo") != offer_id):
        raise BuyerClaimsMismatch("BUYER_TOKEN_IDENTITY_MISMATCH")
    signed_give = claims.get("give")
    signed_take = claims.get("take")
    if not isinstance(signed_give, dict) or not isinstance(signed_take, dict):
        raise BuyerClaimsMismatch("BUYER_TOKEN_ASSET_CLAIMS_MISSING")
    if (signed_give.get("asset") != "KWH" or signed_take.get("asset") != "CR"
            or _amount(signed_give.get("amount"), code="BUYER_SIGNED_GIVE_INVALID") != Decimal(str(quantity))
            or _amount(signed_take.get("amount"), code="BUYER_SIGNED_TAKE_INVALID") != Decimal(str(give_amount))):
        raise BuyerClaimsMismatch("BUYER_TOKEN_AMOUNT_MISMATCH")
    if abs((datetime.fromtimestamp(claims["exp"], timezone.utc) - token_exp).total_seconds()) > 1:
        raise BuyerClaimsMismatch("BUYER_TOKEN_EXPIRY_MISMATCH")
    return {"verified": True, "swapId": swap_id, "sagaExecutionId": saga_execution_id}


def check_buyer_deadline(
    *, closes_at: str, expires_at: str, minimum_remaining_seconds: int,
    saga_execution_id: str, now: datetime,
) -> dict[str, Any]:
    """No usa DB ni red; una ventana insuficiente prohíbe POST /accept."""
    _require(isinstance(saga_execution_id, str) and bool(saga_execution_id),
             "BUYER_DEADLINE_SAGA_REQUIRED")
    _require(type(minimum_remaining_seconds) is int and 0 < minimum_remaining_seconds <= 60,
             "BUYER_DEADLINE_MINIMUM_INVALID")
    moment = _utc(now, "BUYER_DEADLINE_NOW_INVALID")
    close = _utc(closes_at, "BUYER_DEADLINE_CLOSE_INVALID")
    expiry = _utc(expires_at, "BUYER_DEADLINE_EXPIRY_INVALID")
    remain = min((close - moment).total_seconds(), (expiry - moment).total_seconds())
    return {"eligible": remain > minimum_remaining_seconds,
            "reason": "ENOUGH_TIME" if remain > minimum_remaining_seconds else "DEADLINE_TOO_CLOSE"}
