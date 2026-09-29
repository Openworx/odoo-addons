# OW AI Assistant

A clean-room, agentic AI assistant for Odoo 20 Community that works with
any OpenAI-compatible Chat Completions endpoint: OpenRouter (the default),
HostYourAI, OpenAI, a local Ollama, ... Three settings pick the provider
and the model: Base URL, an optional API key and the Default Model. By
Openworx.

## Purpose

`ow_ai` gives internal users an AI agent they can chat with from the
Discuss messaging menu, a systray button, the command palette, or a
"Test in chat" button on the agent record itself. *Composers* configure
each entry point (the systray chat, a chat about a record): which agent
answers, extra instructions and prompt buttons, optionally scoped to a
specific model (e.g. `res.partner`); a chat opened from a record's form
gets that record as context. The agent can read Odoo data, propose and
(after confirmation) apply writes, ask the user questions mid-turn, and
load reusable "skills" (extra instructions/tools loaded on demand).
Everything runs as the requesting user, through Odoo's normal access
rights -- the assistant is not a backdoor.

Each chat has its own switches under "AI chat settings" in the composer's
"More Actions" menu: "Auto-approve actions" (off) and "Show steps" (off:
the "Steps taken" list of the tools the assistant used appears under its
answers once the switch is on, and always in debug mode).

## Requirements

- Odoo 20 Community.
- Python package `markdown2` (used to render the assistant's Markdown
  answers to sanitised HTML; a minimal built-in fallback is used if it is
  missing, with a warning logged at startup).
- `max_cron_threads >= 2` in `odoo.conf` (or `--max-cron-threads`): one
  worker only runs the AI job runner/sweeper and starves every other
  cron job in the database.
- `limit_time_real_cron >= 900`: a single cron run claims and runs jobs
  in a loop (see below) and must not be killed mid-turn.
- An OpenAI-compatible endpoint with a model that supports tool calling,
  plus its API key if the endpoint needs one (see Setup).

## Setup

1. **Provider.** In *Settings > AI > Configuration > Settings* (block
   "AI Provider") set the **Base URL** of the endpoint, its **API Key**
   and the **Default Model** (a model id at that endpoint):

   | Provider | Base URL | Key | Example model |
   |---|---|---|---|
   | OpenRouter (default) | `https://openrouter.ai/api/v1` | `sk-or-…` | `openai/gpt-6-luna` |
   | HostYourAI / Loes (EU) | `https://hostyourai.com/api/v1` | `hyai-…` | `hyai/loes-large` |
   | OpenAI | `https://api.openai.com/v1` | `sk-…` | `gpt-5-mini` |
   | Ollama (local) | `http://ollama:11434/v1` | none | `qwen2.5:3b` |

   The key is optional: leave it empty for an endpoint without
   authentication, and no `Authorization` header is sent. A key saved in
   Settings is stored as the `ow_ai.api_key` system parameter and never
   sent back to the browser (only a `…1234` hint); without one, the
   `OW_AI_API_KEY` environment variable is used (`OPENROUTER_API_KEY` is
   still honoured), then `ow_ai_api_key` in `odoo.conf`. The badge next to
   the field shows where the active key comes from; "Clear key" removes a
   stored key.

   **Test connection** asks the endpoint for its model list (`GET
   /models`) and reports "Connected. N models available.". Some endpoints
   serve that list without checking the key (OpenRouter does), so a wrong
   key there only shows in the first chat. A wrong Base URL gives "The AI
   provider rejected the request." with "Invalid Base URL: ..." (e.g. no
   `http://`/`https://` in front), "The endpoint did not return JSON." (a
   web page instead of the API root) or "HTTP 404"/"Not Found" (no `/v1`
   at the end) instead of a model count; a chat does not retry these.

   Tool calling (every read and write the agent does) and JSON-schema
   output (`_get_structured_response`, for modules building on `ow_ai`)
   depend on the model: pick one that supports both at that endpoint.
   Cost is stored only when the endpoint reports it in the response
   (`usage.cost`). OpenRouter does (without being asked); an endpoint
   that does not (e.g. OpenAI's own API, Ollama) leaves the cost column
   of *Reports > Usage* at 0, and its
   real spend is on the provider's own dashboard. Tokens and latency are
   always recorded.
2. **Default model.** The Default Model (`ow_ai.default_model`) is used
   by every agent that does not set its own model on its record. The
   shipped default is `openai/gpt-6-luna`, an OpenRouter model id: with
   another endpoint, set one of its own models.
   **Model choice (OpenRouter pricing):** `openai/gpt-6-luna` (USD 0.10 /
   0.50 per million input/output tokens, tool calling and JSON-schema
   output) is the cheapest model that passed our end-to-end checks.
   The cheaper `openai/gpt-4.1-nano` and `google/gemini-2.5-flash-lite`
   (0.10 / 0.40) were tried and misused the tools (wrong aggregate/order
   syntax, invented operators and ids, a date filter left out), so their
   answers were wrong. With the default a question costs roughly USD
   0.002 (a simple create) to 0.01 (a multi-step report).
   **Upgrading:** a module update only stores defaults for settings that
   are missing, so a database installed before this default changed keeps
   `openai/gpt-4.1-mini` after a module update; set `ow_ai.default_model`
   to `openai/gpt-6-luna` (or another tool-capable model) yourself in
   *Settings > AI*.
3. Review the other Settings fields (max rounds, max tool calls per
   round, timeout, job staleness, tool result/history size limits) -- the
   defaults are sane for most installs.
4. Install the module, then create or edit agents under *AI > Agents*.

**Upgrading to 20.0.1.2.2.** Fixes an update that failed with
`duplicate key value violates unique constraint
"ir_config_parameter_key_uniq"` after an `ow_ai.*` system parameter was
deleted and created again (a new row the old data file tried to create
again). The defaults are now stored by a function that only fills in
missing settings; nothing to do but update.

**Upgrading to 20.0.1.1.0.** Two safeguards of 20.0.1.0.1 are gone. The
monthly spend cap: set a credit limit on the API key at the provider
instead (OpenRouter: the key's credit limit). The "Deny Data Collection"
flag: set the data policy in the provider's own privacy settings instead
(OpenRouter: the account's privacy settings); prompts include Odoo record
data. Their `ir.config_parameter` rows (`ow_ai.monthly_budget`,
`ow_ai.data_collection_deny`) stay in the database and are ignored.

## Security model

- **Tools run as the requesting user.** Every read/write/introspection
  tool call executes in that user's own environment; Odoo's normal
  access rights, record rules and field-level security apply exactly as
  if the user had made the change by hand. The assistant cannot see or
  touch anything the user could not.
- **Confirmation for writes.** A tool marked `is_write` (creates or
  updates records) pauses the turn and posts a confirmation card; nothing
  is written until the user (or `auto_confirm` on the session, when
  explicitly enabled for that chat) approves it.
- **Model blocklists.** Built-in tools refuse a fixed set of sensitive
  models outright (security and authentication internals, API keys,
  system parameters, the bus, automation rules, `ir.*` and `ow.ai.*`
  models, ...) and keep another set read-only (companies, users, bank
  accounts, messages, ...), regardless of the calling user's own access
  rights. There is no separate field blocklist: field-level security is
  Odoo's own (a field the user cannot read, such as a password hash, is
  just as unreadable to the assistant).
- **Access groups.** *AI User* (implied by every internal user) can chat
  with agents; *AI Manager* additionally configures agents, tools,
  skills and prompts and gets *Reports > Sessions/Jobs/Usage*. The
  conversation transcripts (session events), the session content (tool
  state, request context, a paused batch's tool arguments and results
  and its resume token, the notification buffers, the last error, the
  turn id) and the job data (request context, payload, error detail) are
  readable by
  administrators (*Settings* group) only: AI Managers see the
  operational fields (agent, user, turn state, rounds, settings). The
  chat's own member gets what the chat window needs (the pending card
  and its resume token) from the server with the chat, never by reading
  the session. Only administrators change the AI settings (Base URL,
  API key, default model, limits).
- **Agent contacts.** An agent's contact (its name and avatar in the
  chat) is created by the assistant, archived, when the agent is
  created; a duplicated agent gets a contact of its own. An AI Manager
  cannot bind an agent to an existing contact (only the superuser can,
  e.g. from a data file), and deleting an agent deletes only a contact
  the assistant created for it (flagged *AI Agent Contact*, a flag only
  administrators can read or set), never another one. Older versions
  left no record of which contact they created, so the upgrade flags an
  existing agent's contact only when it looks exactly like one the
  assistant creates (archived, a plain contact without company, parent,
  child contact, user, email, phone or tax id, and no external id of
  another module); any other stays unflagged and survives its agent.
  20.0.1.2.1 takes the flag off contacts that 20.0.1.2.0 flagged without
  that check.
- **One job, one turn.** Every turn gets its own random id (next to the
  round number, which restarts at 1 for every turn of the same user); an
  `agent_round` job records the turn it was queued for and only ever runs
  while the session still waits on that exact turn, so a job left over
  from a turn that already ended or moved on (a slow retry, a crashed
  worker's leftover) can never be mistaken for the session's current one.
  A job queued before jobs carried a turn id never runs (nor is retried)
  once the session has one; the upgrade to 20.0.1.2.1 marks such pending
  or failed jobs that can no longer run as failed, "Superseded by the
  turn-id upgrade".
- **Two-member AI chats.** An AI chat has exactly two members, the user
  and the agent; nobody can invite or add anyone, not through the
  interface and not through RPC. The exception that lets those two
  members be created applies only to the assistant's own (sudo) chat
  setup: the context key it uses does nothing when a client sends it.
- **The API key never reaches the browser.** It is read server-side only
  (from the `ow_ai.api_key` parameter, the `OW_AI_API_KEY`/
  `OPENROUTER_API_KEY` environment variable or `ow_ai_api_key` in
  `odoo.conf`) when a request is made to the AI provider; the config
  form never echoes a previously-saved key back, only a `…1234`-style
  hint. A key from the environment or `odoo.conf` is sent to whatever
  Base URL is configured ("Clear key" only removes a key saved in
  Settings): remove it there before pointing the Base URL at an endpoint
  that must not see it.
- **Exfiltration-safe HTML.** Every bit of HTML the assistant posts goes
  through a single choke point (`engine/html_output.py`) that renders
  Markdown, sanitises the result, and strips anything that could leak
  data through a GET request (tracking pixels, external images/iframes,
  `javascript:`/`url(...)` values, etc.) before it is stored as a
  message. Links to another host are turned into plain text (host shown),
  and CSS classes the model wrote itself are dropped (no fake cards or
  overlays), except a code block's language.

## How the job runner works

Model requests never run inside an HTTP request: posting a message
stores the turn and enqueues an `ow.ai.job` (`kind='agent_round'`), then
returns immediately. Answering a paused confirmation/question/client-tool
card runs the rest of that round's tool batch right in the answering
request (tools only, no model call), then enqueues the next round the
same way.
The `AI: run agent jobs` cron (`ow_ai.ir_cron_job_runner`) is woken up
(`ir.cron._trigger`) and claims jobs one at a time with `FOR UPDATE SKIP
LOCKED`, committing the claim before running the round so a crashed
worker leaves a `running` row a crash cannot hide; each job runs as its
own user (never as the cron's superuser) inside a savepoint and commits
after every job. Only a retryable provider error (a rate limit, a
transport timeout, a provider error such as a 5xx) is retried with
backoff (up to 3 attempts); anything else fails the job and, if it was
the round a session was waiting for, ends that turn with a short,
user-facing apology.

A second cron, `AI: sweep stale jobs` (`ow_ai.ir_cron_job_sweeper`),
fails jobs a dead worker left `running` too long (chat jobs: after
`ow_ai.job_stale_minutes`, 10) or that stayed pending too long (chat
jobs: an hour), expires forgotten confirmation/question cards, and
garbage-collects old finished jobs. The limits are per job kind
(`ow.ai.job._sweep_limits`): modules adding slower kinds give them more
time.

**What to monitor:**

- *Reports > Jobs* (AI Manager) lists every job with its state and
  attempt count (its error detail is shown to administrators only);
  filter on *Failed* to see what needs attention, and use the
  *Retry*/*Cancel* buttons (failed -> pending, requeued through the same
  cron trigger, offered only while the session still waits for that
  round; pending/failed -> cancelled).
- *Reports > Sessions* shows each conversation's `loop_state`, its
  current round vs. round limit, and its `last_error`; a manager can
  *Abort pending* a session stuck waiting on a confirmation/question/
  client-tool result, or *Open chat* to see the conversation in Discuss.
- *Reports > Usage* (list/pivot/graph) tracks tokens, latency and (when
  the endpoint reports it) cost per model, user, agent, kind and company,
  for troubleshooting.

## Troubleshooting

- **No answer arrives.** Check *Reports > Jobs*: if a job is stuck
  `pending`, the cron worker most likely is not running (`max_cron_threads`
  too low, or the cron is disabled) -- also check *Reports > Sessions*
  for a `loop_state` of `waiting_model` that never resolves.
- **A "402"/"payment required" or "401"/"invalid key" apology.** The
  AI provider account is out of credit or the API key is missing/invalid;
  check *Settings > AI > Configuration > Settings* and use "Test
  connection" (it lists the models at the configured endpoint's
  `/models`; some endpoints serve that list without checking the key).
- **"Invalid Base URL" or "The endpoint did not return JSON."** The Base
  URL misses its `http://`/`https://`, or points at a web page instead of
  the API root: use the `/v1` root as in the table under Setup.
- **"HTTP 404" or "Not Found".** The Base URL is missing `/v1` (e.g.
  `http://ollama:11434`): add `/v1`.
- **A job keeps failing and retrying.** Its `error` field (*Reports >
  Jobs*, or the Jobs tab of the session in *Reports > Sessions*; visible
  to administrators) carries the provider's own message/traceback (never
  shown to the end user).

## Extending

- **Register a built-in tool.** Decorate a function with
  `@builtin_tool('your.key', is_write=True/False)` (see
  `engine/tools_registry.py`); then create (or let a data file create) an
  `ow.ai.tool` record with `kind='builtin'` and that `builtin_key`, so an
  agent can be given it as a default tool or load it through a skill.
- **Add a skill.** Create an `ow.ai.skill` record with its own
  instructions and the tools it needs (`tool_ids`); agents that list it
  in `skill_ids` can load it mid-conversation with the built-in
  `interaction.load_skills` tool. A loaded skill's instructions and
  tools then stay available for the rest of that chat session. Native
  skills added by a module update are linked to the default agent
  automatically.
- **Add a job kind.** Extend `ow.ai.job.kind` with `selection_add` and
  define `_run_kind_<kind>(self, env, session)`: `_run_one` calls it as the
  job's user inside a savepoint. Raise `JobFailed(user_message, detail)`
  (`models/ow_ai_job.py`) to fail the job with a short message; an
  `AIProviderError` is handled like in a chat round: only a retryable one
  (rate limit, transport timeout, provider error) is retried, any other
  fails the job. `_enqueue(..., values=)`
  stores extra field values (lookup fields) on the new job, and
  `_enqueue(..., wake=False)` leaves waking the runner cron to the caller
  (one `_wake_runner()` after queuing many jobs). The caller's `default_*`
  context keys never become job or session values. The inline context key
  `ow_ai_inline_jobs` only counts in test mode.
  `ow_ai_automation` uses this for its `field_backfill` and `server_action` jobs.
- **Server-action tools** are not available in this version: the
  `kind='server_action'` value exists on `ow.ai.tool`, but a constraint
  refuses such tools until a later version implements running them.

## Known limitations

- No streaming: an answer appears only once the model round is fully
  done (though intermediate tool-call "steps" are posted as the turn
  progresses).
- No realtime speech-to-text/voice input.
- No tools that read or drive the current Odoo *view* (list/kanban/form
  state); tools operate on data, not on the UI the user happens to be
  looking at.
