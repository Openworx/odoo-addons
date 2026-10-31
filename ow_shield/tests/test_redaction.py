"""Credential redaction of the STORED traffic-log query (round 16 F03).

Rule matching always sees the raw query; waf.hit.query is a copy made
at storage time and must never carry credential VALUES -- while staying
byte-identical for everything else, so an analyst still reads the exact
attack payload the rule engine saw.
"""
from odoo.tests.common import TransactionCase

from ..core.middleware import _redact_query


class TestQueryRedaction(TransactionCase):

    def test_credential_values_are_redacted(self):
        # names match url-decoded, stripped and case-folded; the RAW
        # name is kept so the log still shows which parameter was tried
        for raw in ("password=x", "Password=x", "pass%77ord=x",
                    "PASSWORD =x", "master_pwd=x", "master_password=x",
                    "access_token=x", "api_key=x", "key=x", "token=x",
                    "csrf_token=x", "session_id=x", "sid=x", "pwd=x",
                    "new_password=x", "confirm_password=x", "passwd=x",
                    "authorization=x", "secret=x", "recover_secret=x"):
            out = _redact_query(raw)
            name, sep, value = out.partition("=")
            self.assertEqual(sep, "=", f"{raw} -> {out}")
            self.assertEqual(value, "[redacted]", f"{raw} -> {out}")
            self.assertEqual(name, raw.partition("=")[0],
                             "raw name preserved verbatim")

    def test_everything_else_is_byte_for_byte_untouched(self):
        qs = ("q=1%20UNION%20SELECT%201,2&password=S3cret"
              "&keep=a%2Bb&novalue&empty=&nested?x=1&passwd=Z&key=x")
        self.assertEqual(_redact_query(qs),
                         "q=1%20UNION%20SELECT%201,2&password=[redacted]"
                         "&keep=a%2Bb&novalue&empty=&nested?x=1"
                         "&passwd=[redacted]&key=[redacted]",
                         "order kept, payload kept, pairs without = kept")

    def test_malformed_input_never_raises(self):
        self.assertEqual(_redact_query(""), "")
        self.assertEqual(_redact_query(None), "")
        self.assertEqual(_redact_query("%"), "%")
        self.assertEqual(_redact_query("=&=&%zz="), "=&=&%zz=")
        self.assertEqual(_redact_query("password=" + "%" * 300),
                         "password=[redacted]")
