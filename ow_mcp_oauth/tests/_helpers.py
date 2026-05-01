"""Shared utilities for ow_mcp_oauth HttpCase tests."""
import base64
import hashlib
import json
import secrets

from odoo.addons.ow_mcp_oauth.controllers import _utils as _u


def reset_throttles(env):
    """Clear the DCR throttle ledger so concurrent tests don't trip
    the per-IP limit just by running in sequence.
    """
    _u.dcr_reset(env)


def make_pkce_pair():
    """Return (verifier, challenge) per RFC 7636 with method=S256."""
    verifier = secrets.token_urlsafe(48)[:96]  # 43..128 chars
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


def register_client(case, *, redirect_uri='http://127.0.0.1:9999/callback',
                    auth_method='none', scope='mcp:read mcp:write'):
    """Hit /ow_mcp/oauth/register and return the parsed response."""
    reset_throttles(case.env)
    body = {
        'client_name': 'Test Client',
        'redirect_uris': [redirect_uri],
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'token_endpoint_auth_method': auth_method,
        'scope': scope,
    }
    r = case.url_open(
        '/ow_mcp/oauth/register',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'},
    )
    case.assertEqual(r.status_code, 201, r.text)
    return r.json()


def issue_code_directly(case, *, client, user, scope='mcp:read mcp:write',
                        redirect_uri, code_challenge, resource):
    """Bypass the consent UI by inserting an auth code straight into the DB.

    The /authorize endpoint requires a logged-in browser session which is
    awkward in HttpCase. For unit tests we exercise the post-consent code
    path directly: the issued code can then be exchanged at /token.
    """
    from datetime import datetime, timedelta
    code_plain = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(code_plain.encode('utf-8')).hexdigest()
    case.env['ow.mcp.oauth.authorization_code'].sudo().create({
        'code_hash': code_hash,
        'client_id': client.id,
        'user_id': user.id,
        'redirect_uri': redirect_uri,
        'scope': scope,
        'resource': resource,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'expires_at': datetime.utcnow() + timedelta(seconds=60),
        'consumed': False,
    })
    return code_plain


def post_token(case, **form):
    return case.url_open(
        '/ow_mcp/oauth/token',
        data=form,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
