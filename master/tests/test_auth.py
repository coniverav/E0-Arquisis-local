import time

"""
genera RSA local
token válido/inválido/expirado/
-> 200/401/401/401 contra un endpoint de prueba
"""

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth
from app.main import app

_TEST_KID = "test-key"
_TEST_ISSUER = "https://test-issuer/"
_TEST_AUDIENCE = "test-audience"


def _generate_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_entry(public_key, kid):
    entry = jwt.algorithms.RSAAlgorithm.to_jwk(public_key, as_dict=True)
    entry["kid"] = kid
    entry["alg"] = "RS256"
    entry["use"] = "sig"
    return entry


def _private_pem(private_key):
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _signed_token(private_pem, kid, issuer, audience, lifetime_seconds):
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "test-user",
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + lifetime_seconds,
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


_SIGNING_KEY = _generate_rsa_key()
_OTHER_KEY = _generate_rsa_key()
_JWKS = {"keys": [_jwk_entry(_SIGNING_KEY.public_key(), _TEST_KID)]}
_VALID_TOKEN = _signed_token(
    _private_pem(_SIGNING_KEY), _TEST_KID, _TEST_ISSUER, _TEST_AUDIENCE, 300
)
_EXPIRED_TOKEN = _signed_token(
    _private_pem(_SIGNING_KEY), _TEST_KID, _TEST_ISSUER, _TEST_AUDIENCE, -10
)
_WRONG_KEY_TOKEN = _signed_token(
    _private_pem(_OTHER_KEY), _TEST_KID, _TEST_ISSUER, _TEST_AUDIENCE, 300
)


@app.get("/_test/protected")
def _protected(payload: dict = Depends(auth.verify_jwt)):
    return {"sub": payload.get("sub")}


def _configure(monkeypatch):
    monkeypatch.setattr(
        auth, "AUTH_JWKS_URL", "https://test-issuer/.well-known/jwks.json"
    )
    monkeypatch.setattr(auth, "AUTH_ISSUER", _TEST_ISSUER)
    monkeypatch.setattr(auth, "AUTH_AUDIENCE", _TEST_AUDIENCE)
    monkeypatch.setattr(auth, "fetch_jwks", lambda url: _JWKS)


def test_valid_token(monkeypatch):
    _configure(monkeypatch)
    response = TestClient(app).get(
        "/_test/protected", headers={"Authorization": f"Bearer {_VALID_TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json() == {"sub": "test-user"}


def test_invalid_token(monkeypatch):
    _configure(monkeypatch)
    response = TestClient(app).get(
        "/_test/protected", headers={"Authorization": f"Bearer {_WRONG_KEY_TOKEN}"}
    )
    assert response.status_code == 401


def test_expired_token(monkeypatch):
    _configure(monkeypatch)
    response = TestClient(app).get(
        "/_test/protected", headers={"Authorization": f"Bearer {_EXPIRED_TOKEN}"}
    )
    assert response.status_code == 401


def test_missing_token(monkeypatch):
    _configure(monkeypatch)
    response = TestClient(app).get("/_test/protected")
    assert response.status_code == 401

def test_missing_auth_configuration_returns_503(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_JWKS_URL", None)
    monkeypatch.setattr(auth, "AUTH_ISSUER", None)
    monkeypatch.setattr(auth, "AUTH_AUDIENCE", None)

    response = TestClient(app).get(
        "/_test/protected",
        headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
    )

    assert response.status_code == 503

def test_wrong_issuer_returns_401(monkeypatch):
    _configure(monkeypatch)

    token = _signed_token(
        _private_pem(_SIGNING_KEY),
        _TEST_KID,
        "https://wrong-issuer/",
        _TEST_AUDIENCE,
        300,
    )

    response = TestClient(app).get(
        "/_test/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_wrong_audience_returns_401(monkeypatch):
    _configure(monkeypatch)

    token = _signed_token(
        _private_pem(_SIGNING_KEY),
        _TEST_KID,
        _TEST_ISSUER,
        "wrong-audience",
        300,
    )

    response = TestClient(app).get(
        "/_test/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
