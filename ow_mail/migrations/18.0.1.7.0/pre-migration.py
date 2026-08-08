def migrate(cr, version):
    """Pre-create ``signature_odoo`` so existing custom signatures survive.

    ``signature_html`` becomes a stored computed field in this version and
    ``signature_odoo`` defaults to True. Without this hook the upgrade would
    recompute every account's signature from the Odoo user signature,
    overwriting hand-written ones. Creating and filling the column up front
    means the ORM applies no default and the compute keeps custom signatures
    (signature_odoo = False) intact.
    """
    cr.execute(
        "ALTER TABLE ow_mail_account ADD COLUMN IF NOT EXISTS signature_odoo boolean"
    )
    cr.execute(
        """
        UPDATE ow_mail_account
           SET signature_odoo = (signature_html IS NULL OR signature_html = '')
        """
    )
