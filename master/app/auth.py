import time
import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from .config import AUTH_AUDIENCE, AUTH_ISSUER, AUTH_JWKS_URL

"""
verify_jwt como Depends
fetch JWKS con httpx + caché en memoria
valida firma/issuer/audience/exp
401 sin token o inválido
"""

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

_jwks_cache = {}
_JWKS_TTL_SECONDS = 600


def fetch_jwks(jwks_url):
    # Implementar caché en memoria para el JWKS
    now = time.time()
    cached = _jwks_cache.get(jwks_url)
    if cached is not None and now - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]
    try:
        response = httpx.get(jwks_url, timeout=10)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Unable to fetch JWKS")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="Unable to fetch JWKS")
    jwks = response.json()
    _jwks_cache[jwks_url] = (now, jwks)
    return jwks


def _public_key_for_kid(jwks, kid):
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            try:
                return jwt.algorithms.RSAAlgorithm.from_jwk(key)
            except Exception:
                return None
    return None


def verify_jwt(token: str = Depends(oauth2_scheme)):
    if not AUTH_JWKS_URL or not AUTH_ISSUER or not AUTH_AUDIENCE:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    # Fetch JWKS del identit provider (httpx + cache en memoria)
    jwks = fetch_jwks(AUTH_JWKS_URL)
    # Decode el JWT sin obtener el header verificado para obtener el "kid"
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    # Encontrar la key publica correspondiente al "kid" en el JWKS
    public_key = _public_key_for_kid(jwks, unverified_header.get("kid"))
    if public_key is None:
        raise HTTPException(status_code=401, detail="Unknown key")
    try:
        # Verificar la firma JWT y validar claims (audience, issuer, exp)
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=AUTH_AUDIENCE,
            issuer=AUTH_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
