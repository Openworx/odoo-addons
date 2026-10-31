import logging

_logger = logging.getLogger(__name__)

from . import core
from . import models
from . import controllers
# do NOT import .tests here: odoo.tests.loader imports this package
# itself when running tests, and importing it on every boot pollutes
# production logs ("Importing test framework" ERROR)
from .hooks import post_init_hook  # noqa: PLC0415  (manifest hook entry)


def _install_middleware():
    """Wrap odoo.http.Application.__call__ (idempotent, per process)."""
    try:
        core.middleware.install()
    except Exception:  # pragma: no cover - never break the server on import
        _logger.exception("OW Shield: middleware install failed")


_install_middleware()
