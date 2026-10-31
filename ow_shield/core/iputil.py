"""Pure-stdlib client IP resolution and network matching.

Trust model (deployments where nginx terminates TLS on localhost):
  * the socket peer (REMOTE_ADDR) is the only inherently trusted source.
  * forwarding headers (X-Real-IP, X-Forwarded-For) count only when the
    socket peer is inside one of the trusted proxy CIDRs.
  * X-Real-IP wins over X-Forwarded-For: proxies that set it via
    ``proxy_set_header X-Real-IP $remote_addr`` overwrite any forged
    value, and with nginx ``real_ip`` (proxy_protocol) it holds the true
    client.
  * XFF is walked right-to-left, skipping trusted proxies; the leftmost
    entry is attacker-controllable and is never trusted on its own.
"""
import ipaddress


def parse_cidrs(spec):
    """Parse a comma/space/newline separated list of CIDRs or bare IPs."""
    nets = []
    for chunk in (spec or "").replace("\n", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            continue
    return nets


def parse_ip(value):
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except (ValueError, AttributeError):
        return None


def in_any(ip_obj, networks):
    return any(ip_obj in net for net in networks)


def split_xff(xff_header):
    """Ordered (left-to-right) list of valid IPs in an XFF header."""
    out = []
    for token in (xff_header or "").split(","):
        ip_obj = parse_ip(token)
        if ip_obj:
            out.append(ip_obj)
    return out


def resolve_client_ip(remote_addr, x_real_ip, x_forwarded_for, trusted_networks):
    """Return the effective client IP (str) for this request.

    :param remote_addr: environ REMOTE_ADDR (socket peer)
    :param x_real_ip: X-Real-IP header value or None
    :param x_forwarded_for: X-Forwarded-For header value or None
    :param trusted_networks: list of ipaddress networks considered proxies
    """
    remote = parse_ip(remote_addr)
    if remote is None:
        return None
    if not in_any(remote, trusted_networks):
        # Direct connection (e.g. someone bypassing nginx): the socket peer
        # IS the client; forwarding headers are meaningless/spoofable.
        return str(remote)
    xri = parse_ip(x_real_ip)
    if xri is not None and not in_any(xri, trusted_networks):
        return str(xri)
    # XFF: walk right-to-left, skipping entries that are themselves trusted
    # proxies; the first remaining entry is the client as seen by nginx.
    for hop in reversed(split_xff(x_forwarded_for)):
        if not in_any(hop, trusted_networks):
            return str(hop)
    if xri is not None:
        return str(xri)
    return str(remote)
