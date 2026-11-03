"""Records that belong to one user, and must stay with them."""
from odoo import models


class OwMailOwnedMixin(models.AbstractModel):
    """Re-check the "own only" record rule after a write that changes the owner.

    Odoo checks record rules before a write, against the record as it is,
    and not again afterwards. A user could therefore write the owner field
    of a record of their own and hand it to another user: an account on a
    server they control would appear in the colleague's client as one of
    the colleague's mailboxes. Checking again once the owner has changed
    rejects that, and rolls the write back with the transaction.

    The superuser is not subject to record rules, so code running with
    ``sudo()`` can still reassign records.
    """

    _name = "ow.mail.owned.mixin"
    _description = "OW Mail: record owned by one user"

    # The field the record's "own only" rule follows.
    _ow_owner_field = "user_id"

    def write(self, vals):
        res = super().write(vals)
        if self._ow_owner_field in vals:
            self.check_access("write")
        return res
