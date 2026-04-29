# OW Mail tests

Unit tests (pure) and integration tests (hit the GreenMail container
from the Compose stack).

## Run

From the repo root:

```bash
docker compose exec -T odoo odoo -d ow_test -u ow_mail \
  --stop-after-init --test-enable --test-tags ow_mail \
  --db_host=db --db_user=odoo --db_password=odoo
```

Integration tests that need GreenMail self-skip when the container
isn't reachable on ``greenmail:3143`` — so the parser unit tests still
run in barebones environments.

## Layout

- `test_search_dsl.py` — DSL parser cases (no IMAP, no HTTP).
- `test_messages_route.py` — seeds GreenMail with fixture messages,
  POSTs to `/ow_mail/messages` with DSL queries, asserts the UID set.
- `helpers.py` — GreenMail reachability check + `greenmail_append` /
  `clear_inbox` primitives built on `imaplib`.
