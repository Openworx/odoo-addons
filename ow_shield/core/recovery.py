"""Signed recovery tokens so an admin locked out by an auto-lockout can
un-block themselves through a short-lived URL emailed to the alert address.

A token is bound to (database, network), expires after 15 minutes, and
the controller only ever deactivates the automatic deny entry it names,
so replays gain nothing beyond that and no token store is needed.
"""
import hashlib
import hmac
import time

SEP = "~"


def make_token(secret, dbname, ip, ttl_minutes=15):
    expires = int(time.time()) + ttl_minutes * 60
    payload = SEP.join([dbname, ip, str(expires)])
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}{SEP}{sig}"


def check_token(secret, token, now=None):
    """Return (dbname, ip) when the token is valid and unexpired."""
    parts = (token or "").split(SEP)
    if len(parts) != 4:
        return None
    dbname, ip, expires, sig = parts
    payload = SEP.join([dbname, ip, expires])
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        if (now or time.time()) > int(expires):
            return None
    except ValueError:
        return None
    return dbname, ip
