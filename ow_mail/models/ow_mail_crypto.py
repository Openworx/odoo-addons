"""Symmetric encryption helper for stored IMAP/SMTP passwords.

Uses Fernet (AES-128-CBC + HMAC) from the `cryptography` package shipped with
Odoo. The key is stored in `ir.config_parameter` under
`ow_mail.cipher_key` and generated lazily on first use.

**Key rotation**: there is no automatic re-encryption path. If the key is
lost or replaced, every previously-encrypted password becomes undecryptable.
To rotate safely:

  1. Copy the existing key out of ``ir.config_parameter`` (key
     ``ow_mail.cipher_key``).
  2. For each account, re-enter the password via the UI (this writes
     fresh ciphertext with the current key).
  3. Only then remove / replace the key.

If step 1/2 are skipped, the next ``_get_imap_password`` / ``send_mail``
call raises ``CipherKeyError`` so the user sees a clear failure instead
of a silent empty-password login attempt (which servers tend to treat as
an auth failure and some treat as a lock-out event).
"""
import base64
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_KEY = "ow_mail.cipher_key"


class CipherKeyError(UserError):
    """Raised when stored ciphertext can't be decrypted with the current key."""


class OwMailCrypto(models.AbstractModel):
    _name = "ow.mail.crypto"
    _description = "OW Mail Crypto Helper"

    def _get_fernet(self):
        ICP = self.env["ir.config_parameter"].sudo()
        key = ICP.get_param(PARAM_KEY)
        if not key:
            key = base64.urlsafe_b64encode(os.urandom(32)).decode()
            ICP.set_param(PARAM_KEY, key)
        return Fernet(key.encode())

    def encrypt(self, plaintext):
        """Encrypt *plaintext* with Fernet (AES-128-CBC + HMAC-SHA256).

        Returns a base64 URL-safe token string, or ``False`` for empty input.
        ``_get_fernet()`` is called on each invocation so that any in-place
        key rotation (replacing the ``ir.config_parameter`` value) is
        automatically picked up without restarting the server.
        """
        if not plaintext:
            return False
        return self._get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token, strict=False):
        """Return the plaintext for *token*.

        When *strict* is True, an undecryptable token raises
        ``CipherKeyError`` — use this from code paths where handing an
        empty password to the server would do real damage (login loops,
        account lockout). The non-strict default preserves the legacy
        behaviour where missing/corrupt tokens are treated as "no
        password set" and callers surface their own error.
        """
        if not token:
            return ""
        try:
            return self._get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError) as e:
            _logger.warning(
                "ow_mail: could not decrypt stored password — "
                "Fernet key at ir.config_parameter '%s' has changed or "
                "ciphertext is corrupt (%s)",
                PARAM_KEY, e.__class__.__name__,
            )
            if strict:
                raise CipherKeyError(_(
                    "Stored credential cannot be decrypted. The encryption "
                    "key (ir.config_parameter '%s') has changed or the "
                    "ciphertext is corrupt. Re-enter the password on the "
                    "account form to re-encrypt with the current key."
                ) % PARAM_KEY)
            return ""
