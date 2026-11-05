# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

SEPA_ODOO_METHOD_CODE = "sepa_direct_debit"

SEPA_MOLLIE_METHOD_ID = "directdebit"

SEPA_FIRST_PAYMENT_METHOD_CODES = [
    "bancontact",
    "belfius",
    "eps",
    "ideal",
    "kbc",
    "mybank",
    "paybybank",
]

MOLLIE_SEPA_MIN_AMOUNT = 0.01
# Mollie minimum amounts for a mandate first payment.
SEPA_MANDATE_DEFAULT_AMOUNT = 0.01
SEPA_MANDATE_AMOUNTS = {"bancontact": 0.02}
MOLLIE_SEPA_DEFAULT_MAX_AMOUNT = 1000.00

LINE_STATE_MAPPING = {
    "draft": "draft",
    "pending": "pending",
    "authorized": "pending",
    "done": "done",
    "cancel": "cancel",
    "error": "failed",
}

ACTIVE_LINE_STATES = ("draft", "pending")

# Max page size allowed by Mollie.
MOLLIE_LIST_LIMIT = 250

CRON_TIME_MARGIN = 60
