"""Input normalization to defeat simple WAF evasion.

Rules match against the normalized form. Cheap by design: a couple of
percent-decodes, null-byte/control stripping, whitespace collapsing and
casefolding.
"""
from urllib.parse import unquote


def normalize(text, decode_plus=False, max_len=262144):
    if not text:
        return ""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="replace")
    text = text[:max_len]
    if decode_plus:
        # query-string convention: a raw '+' means space, while '%2B'
        # must survive decoding as a literal plus (doing this AFTER
        # percent-decoding would also eat attacker-encoded operators)
        text = text.replace("+", " ")
    # iterative percent-decoding (catches %252e%252e double encoding)
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    # null bytes become the literal marker %00 so rules can match them
    text = text.replace("\x00", "%00")
    # unicode normalization of common evasion tricks
    text = (
        text.replace("\u00a0", " ")   # nbsp
        .replace("\u200b", "")        # zero width space
        .replace("\ufeff", "")        # bom
    )
    text = "".join(ch if (ch >= " " or ch in "\t\n\r") else " " for ch in text)
    # collapse whitespace runs (also defeats comment/newline splitting of
    # payloads like "UNION/**/SELECT" -- collapse /**/ too)
    for _ in range(3):
        collapsed = " ".join(text.replace("/**/", " ").split())
        if collapsed == text:
            break
        text = collapsed
    return text.casefold()


# values are machine-generated protocol noise, never attack payloads;
# everything else (User-Agent, Cookie, Authorization, custom X-*) is
# scanned: Log4Shell-class payloads arrive in *any* header.
HEADER_SKIP = frozenset({
    "sec-websocket-key", "sec-websocket-protocol", "sec-websocket-version",
    "sec-websocket-extensions", "connection", "upgrade",
    "upgrade-insecure-requests", "dnt", "content-length", "accept-encoding",
    "priority", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-fetch-user", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "cache-control", "pragma", "te", "keep-alive", "trans-encoding",
})


IP_LIST_HEADERS = ("x-real-ip", "x-forwarded-for")


def _clean_ip_list(value, trusted):
    """Collapse a merged comma-separated proxy header to its meaningful
    values.

    WSGI merges duplicate header lines into "a, b". Load-balancer chains
    (LB + nginx both setting X-Real-IP from the same PROXY-protocol
    peer) legitimately emit "client, client" on every request; identical
    or empty segments carry no smuggling ambiguity, so they must not
    trip the multiple-value rules. Hops inside the operator's trusted
    proxy CIDRs are own infrastructure: only foreign extra values (the
    smuggling shape) survive.
    """
    if "," not in value:
        return value
    from .iputil import in_any, parse_ip
    kept, seen = [], set()
    for seg in value.split(","):
        s = seg.strip()
        if not s or s in seen:
            continue
        if trusted:
            ip_obj = parse_ip(s)
            if ip_obj is not None and in_any(ip_obj, trusted):
                continue
        seen.add(s)
        kept.append(s)
    return ", ".join(kept)


def normalize_headers(environ, names=(), trusted=None, value_cap=1024,
                      total_cap=8192):
    """Return a single lowercased blob of header values for header-scope
    rules. ``names`` are scanned first verbatim (proxy-smuggling rules);
    after that every other HTTP_* header joins the blob within caps.
    ``trusted`` (parsed CIDR networks) removes own-infrastructure hops
    from the forwarding headers before matching.
    """
    parts = []
    total = 0
    seen = set()
    for name in names:
        key = name.upper().replace("-", "_")
        value = environ.get("HTTP_" + key)
        seen.add(key)
        if value and name.lower() in IP_LIST_HEADERS:
            value = _clean_ip_list(value, trusted)
        if value:
            part = f"{name.lower()}:{normalize(value)}"
            parts.append(part)
            total += len(part)
    for key, value in environ.items():
        if not key.startswith("HTTP_") or key[5:] in seen:
            continue
        name = key[5:].lower().replace("_", "-")
        if name in HEADER_SKIP or not value:
            continue
        part = f"{name}:{normalize(value, max_len=value_cap)}"
        parts.append(part)
        total += len(part)
        if total >= total_cap:
            break
    return "\n".join(parts)
