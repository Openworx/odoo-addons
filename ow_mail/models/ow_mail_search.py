"""Gmail-style search DSL → IMAP SEARCH criteria.

Turns a query like::

    from:boss@x.com subject:"weekly report" has:attachment before:2026-04-01 label:work

into a pair ``(criteria, post_filters)`` where ``criteria`` is an IMAP
SEARCH string (RFC 3501) and ``post_filters`` is a dict of client-side
filters that IMAP can't express (currently only ``has_attachment``).

Pure function — no IMAP, no DB — so it's trivially unit-testable.
The ``tag_lookup`` callback resolves ``label:<name>`` into an IMAP
keyword string; pass ``None`` to skip label support.
"""
import re
import shlex
from datetime import date, datetime, timedelta

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_OP_PATTERN = re.compile(
    r"^(from|to|cc|subject|body|label|has|is|before|since|on|older_than|newer_than):",
    re.I,
)

_IS_MAP = {
    "unread": "UNSEEN",
    "read": "SEEN",
    "starred": "FLAGGED",
    "unstarred": "UNFLAGGED",
}


# Control characters are illegal in an IMAP quoted-string and a raw CR/LF
# would terminate the command line, so anything after it is read by the
# server as a fresh command. imaplib does no filtering of its own.
_CTL_RX = re.compile(r"[\x00-\x1f\x7f]")


def _imap_quote(s):
    """Escape a value for use inside an IMAP SEARCH quoted string.

    IMAP SEARCH arguments that contain spaces must be passed as quoted strings.
    Per RFC 3501, both backslash and double-quote must be escaped with a leading
    backslash inside the quoted-string syntax.

    Control characters are dropped before escaping — that is the command
    injection vector. Unlike ``imap_mbox_quote``, which raises, this strips:
    the input here is a search box, where a stray character should narrow
    the search rather than fail the request.
    """
    clean = _CTL_RX.sub("", s or "")
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fmt_imap_date(d):
    """Format a ``datetime.date`` as IMAP DD-Mon-YYYY (e.g. ``01-Jan-2024``).

    IMAP SEARCH uses this specific format rather than ISO 8601. The month
    abbreviation is taken from the hard-coded ``_MONTHS`` list to avoid
    locale-sensitive output from ``strftime``.
    """
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _parse_ymd(s):
    """Parse a YYYY-MM-DD string to a ``datetime.date``. Returns None on failure."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _tokenize(q):
    """Split the query into tokens, honoring double-quoted strings.

    Operator prefixes (``from:"foo bar"``) stay attached to the
    quoted value.
    """
    lex = shlex.shlex(q, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        bare = list(lex)
    except ValueError:
        # Unterminated quote or similar — fall back to whitespace split.
        bare = q.split()
    # Re-attach operator prefixes that shlex split off when the value was
    # quoted (shlex passes `from:foo bar` through as-is; the trouble is
    # when a user writes `from:"foo bar"` — shlex yields `from:foo bar`).
    # Using posix=True + whitespace_split keeps `from:foo bar` intact if
    # the operator sat inside the quote, so no further work is needed.
    return bare


def _day_offset(today, token):
    """Parse `Nd` into a timedelta. Returns None on anything unparseable."""
    m = re.fullmatch(r"(\d+)d", token, re.I)
    if not m:
        return None
    return timedelta(days=int(m.group(1)))


def parse_query(q, tag_lookup=None, today=None):
    """Parse a DSL query.

    Returns ``(criteria_str, post_filters)``:

    * ``criteria_str`` — IMAP SEARCH criteria (may be ``""`` if nothing
      mapped cleanly).
    * ``post_filters`` — dict of envelope-level filters to apply after
      fetch. Currently supports ``{"has_attachment": True}``.
    """
    if not q or not q.strip():
        return "", {}

    today = today or date.today()
    tokens = _tokenize(q)

    criteria = []
    post_filters = {}
    free_text = []

    for tok in tokens:
        if not tok:
            continue
        m = _OP_PATTERN.match(tok)
        if not m:
            free_text.append(tok)
            continue
        op = m.group(1).lower()
        value = tok[len(m.group(0)):]  # strip `op:`
        # Strip surrounding quotes if shlex left any; shouldn't happen
        # with posix=True, but harmless.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not value:
            continue

        if op == "from":
            criteria.append(f"FROM {_imap_quote(value)}")
        elif op == "to":
            criteria.append(f"TO {_imap_quote(value)}")
        elif op == "cc":
            criteria.append(f"CC {_imap_quote(value)}")
        elif op == "subject":
            criteria.append(f"SUBJECT {_imap_quote(value)}")
        elif op == "body":
            criteria.append(f"BODY {_imap_quote(value)}")
        elif op == "label":
            kw = tag_lookup(value) if tag_lookup else None
            if kw:
                criteria.append(f"KEYWORD {kw}")
            # Unknown labels just drop out (strict behaviour).
        elif op == "is":
            imap = _IS_MAP.get(value.lower())
            if imap:
                criteria.append(imap)
        elif op == "has":
            if value.lower() in ("attachment", "att", "attachments"):
                post_filters["has_attachment"] = True
            # other has:* values are ignored for now.
        elif op == "before":
            d = _parse_ymd(value)
            if d:
                criteria.append(f"BEFORE {_fmt_imap_date(d)}")
            else:
                free_text.append(tok)
        elif op == "since":
            d = _parse_ymd(value)
            if d:
                criteria.append(f"SINCE {_fmt_imap_date(d)}")
            else:
                free_text.append(tok)
        elif op == "on":
            d = _parse_ymd(value)
            if d:
                criteria.append(f"ON {_fmt_imap_date(d)}")
            else:
                free_text.append(tok)
        elif op == "older_than":
            delta = _day_offset(today, value)
            if delta:
                criteria.append(f"BEFORE {_fmt_imap_date(today - delta)}")
            else:
                free_text.append(tok)
        elif op == "newer_than":
            delta = _day_offset(today, value)
            if delta:
                criteria.append(f"SINCE {_fmt_imap_date(today - delta)}")
            else:
                free_text.append(tok)

    # Free text: AND each token as TEXT "..." (matches header + body on
    # any compliant IMAP server; single-quoted for safety).
    for t in free_text:
        criteria.append(f"TEXT {_imap_quote(t)}")

    return " ".join(criteria), post_filters


def has_operators(q):
    """Does the query contain at least one known DSL operator?

    Used by the controller to decide whether to skip the legacy
    substring fallback (which would loosen strict operators like
    `from:boss@x.com`).
    """
    if not q:
        return False
    for tok in _tokenize(q):
        if _OP_PATTERN.match(tok):
            return True
    return False
