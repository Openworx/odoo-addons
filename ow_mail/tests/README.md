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
- `test_record_link.py` — create-record-from-email helper: curated menu
  filtering, prefill mapping, attach + Message-ID stamping, linked-records
  lookup (pure ORM; IMAP mocked via the `_fetch_raw` seam).
- `test_record_routes.py` — GreenMail round-trip of
  `/ow_mail/record/prefill` → `/ow_mail/record/attach` →
  `linked_records` in `/ow_mail/message`.
- `helpers.py` — GreenMail reachability check + `greenmail_append` /
  `clear_inbox` primitives built on `imaplib`.
