"""RSA keypair + RS256 JWT helpers for ow_mcp_oauth.

Wraps PyJWT and `cryptography` so callers don't import them directly.

Key model:
  - The active `ow.mcp.oauth.config` singleton stores one current RSA private
    key (PEM) plus its kid, and optionally one previous public key (PEM) +
    kid for rotation grace. New tokens are signed with the current key;
    verification accepts both.
  - `kid` is a short stable identifier (uuid4 hex truncated to 16 chars).

Token shape (RFC 9068 — JWT Profile for OAuth 2.0 Access Tokens):
    header  = {"alg": "RS256", "typ": "at+jwt", "kid": "<kid>"}
    payload = {iss, sub, aud, client_id, iat, exp, jti, scope}
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import uuid

_logger = logging.getLogger(__name__)


def _b64url_uint(n: int) -> str:
    """Base64url-encode an unsigned integer per RFC 7518 §6.3.1."""
    length = (n.bit_length() + 7) // 8
    raw = n.to_bytes(length, 'big') if length else b'\x00'
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def generate_rsa_keypair():
    """Return (private_pem: str, public_pem: str, kid: str)."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode('ascii')
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode('ascii')
    kid = uuid.uuid4().hex[:16]
    return priv, pub, kid


def public_pem_from_private_pem(private_pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        private_pem.encode('ascii'), password=None,
    )
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode('ascii')


def jwk_from_public_pem(public_pem: str, kid: str) -> dict:
    """Build a JWK dict (RFC 7517) from a public RSA PEM."""
    from cryptography.hazmat.primitives import serialization

    pub = serialization.load_pem_public_key(public_pem.encode('ascii'))
    numbers = pub.public_numbers()
    return {
        'kty': 'RSA',
        'use': 'sig',
        'alg': 'RS256',
        'kid': kid,
        'n': _b64url_uint(numbers.n),
        'e': _b64url_uint(numbers.e),
    }


def sign_access_token(
    private_pem: str,
    kid: str,
    *,
    issuer: str,
    subject: str,
    audience: str,
    client_id: str,
    scope: str,
    ttl_seconds: int,
    extra_claims: dict | None = None,
) -> tuple[str, str, int, int]:
    """Mint a signed RS256 JWT.

    Returns (token, jti, iat, exp).
    """
    import time

    import jwt

    iat = int(time.time())
    exp = iat + int(ttl_seconds)
    jti = secrets.token_urlsafe(24)
    payload = {
        'iss': issuer,
        'sub': str(subject),
        'aud': audience,
        'client_id': client_id,
        'iat': iat,
        'exp': exp,
        'jti': jti,
        'scope': scope or '',
    }
    if extra_claims:
        payload.update(extra_claims)
    headers = {'kid': kid, 'typ': 'at+jwt'}
    token = jwt.encode(payload, private_pem, algorithm='RS256', headers=headers)
    if isinstance(token, bytes):
        token = token.decode('ascii')
    return token, jti, iat, exp


def decode_token_unverified_header(token: str) -> dict:
    import jwt

    try:
        return jwt.get_unverified_header(token)
    except Exception:
        return {}


def verify_access_token(
    token: str,
    *,
    public_pems_by_kid: dict[str, str],
    issuer: str,
    audience: str,
    leeway: int = 30,
) -> dict | None:
    """Verify signature + standard claims (iss, aud, exp, iat, nbf).

    Returns the payload dict or None on any failure. Audience MUST match
    exactly (RFC 8707 / MCP confused-deputy defense).
    """
    import jwt
    from jwt import InvalidTokenError

    header = decode_token_unverified_header(token)
    kid = header.get('kid')
    if not kid or kid not in public_pems_by_kid:
        return None
    public_pem = public_pems_by_kid[kid]
    try:
        payload = jwt.decode(
            token,
            public_pem,
            algorithms=['RS256'],
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            options={
                'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'jti'],
            },
        )
    except InvalidTokenError as e:
        _logger.debug('JWT verify failed: %s', e)
        return None
    if payload.get('aud') != audience:
        return None
    return payload
