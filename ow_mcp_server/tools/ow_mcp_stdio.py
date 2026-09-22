#!/usr/bin/env python3
"""
ow_mcp_stdio — MCP stdio ⇄ HTTP bridge for Odoo's ow_mcp_server.

Usage:
    ow_mcp_stdio.py --url https://odoo.example.com/mcp --api-key $ODOO_MCP_KEY
    ow_mcp_stdio.py --url ... --api-key ... --database my_db   # multi-db

The API key must be created in Odoo (My Profile → Account Security → New API
Key) with scope set to "mcp". The /mcp endpoint accepts Bearer tokens only;
there is no user/password mode.

Env-var equivalents: ODOO_MCP_KEY, ODOO_DB.

Reads newline-delimited JSON-RPC from stdin, POSTs each message to the remote
/mcp endpoint, writes the JSON response to stdout (one JSON object per line).
Notifications (no "id") are POSTed but produce no stdout.

Only standard-library imports — no pip dependencies. Compatible with
Python 3.9+ (macOS system Python) thanks to the future annotations import.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def build_url(base_url: str, database: str | None) -> str:
    if not database:
        return base_url
    parts = urllib.parse.urlsplit(base_url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query['db'] = database
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query))
    )


def post(url, headers, body):
    req = urllib.request.Request(
        url, data=body.encode('utf-8'), method='POST', headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.read().decode('utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--url', required=True, help='https://host/mcp')
    ap.add_argument(
        '--api-key',
        default=os.environ.get('ODOO_MCP_KEY'),
        help='Odoo API key with scope=mcp (or set $ODOO_MCP_KEY).',
    )
    ap.add_argument(
        '--database',
        default=os.environ.get('ODOO_DB'),
        help='Target Odoo database name for multi-db servers (or set $ODOO_DB).',
    )
    args = ap.parse_args()
    if not args.api_key:
        raise SystemExit(
            'ow_mcp_stdio: --api-key is required (or set $ODOO_MCP_KEY)'
        )

    url = build_url(args.url, args.database)
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {args.api_key}',
    }
    if args.database:
        # X-Odoo-Database — read by ow_mcp_server's controller for verification.
        headers['X-Odoo-Database'] = args.database
        # X-Odoo-Dbfilter — honored by OCA's dbfilter_from_header module to
        # select the target DB per request. Value is a regex matched against
        # the DB name; use an anchored exact match here.
        headers['X-Odoo-Dbfilter'] = f'^{args.database}$'

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        is_notification = isinstance(msg, dict) and 'id' not in msg
        resp = post(url, headers, line)
        if is_notification:
            continue
        sys.stdout.write(resp.rstrip('\n') + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
