"""Test helpers for ow_mail — GreenMail IMAP priming + reachability.

The Docker-Compose dev stack runs a GreenMail container (see
CLAUDE.md); from inside the Odoo container the host is ``greenmail``
on port 3143 (IMAP, plaintext) with credentials ``demo`` / ``demo``.
"""
import imaplib
import socket
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

GREENMAIL_HOST = "greenmail"
GREENMAIL_IMAP_PORT = 3143
GREENMAIL_SMTP_PORT = 3025
GREENMAIL_USER = "demo"
GREENMAIL_PASS = "demo"


def greenmail_reachable(host=GREENMAIL_HOST, port=GREENMAIL_IMAP_PORT, timeout=0.5):
    """Quick TCP probe so integration tests can self-skip when the
    container isn't running (unit runners, CI without compose, …)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _build_message(frm, to, subject, html=None, text=None, attachments=None,
                   date_utc=None):
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(
        timeval=(date_utc or datetime.now(timezone.utc)).timestamp(),
        localtime=False)
    msg["Message-ID"] = make_msgid(domain="test.ow.test")
    msg.set_content(text or subject or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        msg.add_attachment(
            att["content"],
            maintype=att.get("maintype", "application"),
            subtype=att.get("subtype", "octet-stream"),
            filename=att["filename"],
        )
    return msg


def greenmail_append(folder="INBOX", frm="Sender <sender@test.ow.test>",
                     to=None, subject="", html=None, text=None,
                     attachments=None, date_utc=None,
                     host=GREENMAIL_HOST, port=GREENMAIL_IMAP_PORT,
                     user=GREENMAIL_USER, password=GREENMAIL_PASS):
    """Append a message to GreenMail. Returns the Message-ID header."""
    to = to or f"{user}@ow.test"
    msg = _build_message(frm, to, subject, html=html, text=text,
                         attachments=attachments, date_utc=date_utc)
    conn = imaplib.IMAP4(host, port)
    conn.login(user, password)
    try:
        internaldate = imaplib.Time2Internaldate(
            date_utc or datetime.now(timezone.utc))
        typ, _ = conn.append(folder, "", internaldate, msg.as_bytes())
        if typ != "OK":
            raise RuntimeError("IMAP APPEND failed")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return msg["Message-ID"]


def clear_inbox(folder="INBOX",
                host=GREENMAIL_HOST, port=GREENMAIL_IMAP_PORT,
                user=GREENMAIL_USER, password=GREENMAIL_PASS):
    """Flag-delete every message in ``folder`` then EXPUNGE."""
    conn = imaplib.IMAP4(host, port)
    conn.login(user, password)
    try:
        typ, _ = conn.select(folder)
        if typ != "OK":
            return
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return
        uids = data[0].split()
        if not uids:
            return
        conn.uid("STORE", b",".join(uids), "+FLAGS", "(\\Deleted)")
        conn.expunge()
        conn.close()
    finally:
        try:
            conn.logout()
        except Exception:
            pass
