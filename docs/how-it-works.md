# How it works

Reference material for the outreach assistant: how company research and drafting work, why things are built
the way they are, and what the tests cover. To install and use the tool, start with the
[README](../README.md).

```
extension/  Chrome MV3 side panel (plain JS, no build step)
backend/    FastAPI + SQLite: prompts, your context, history, LLM calls, dashboard (Jinja2 + HTMX + Chart.js)
docker-compose.yml, .env.example
```

The extension's settings are stored in `chrome.storage.local`. The current draft is kept in
`chrome.storage.session`, so it survives closing the panel and is cleared when the browser closes.

The backend is the only component that calls the LLM. Calls go to `POST {LLM_BASE_URL}/chat/completions`
with `Authorization: Bearer {LLM_API_KEY}`. API docs are served at <http://localhost:8765/api/docs>.

## Company research

- **Which company.** In order:
  1. what you typed under "Wrong company?";
  2. the "Current company" label on the page;
  3. the headline (`… at Acme`, `… chez Acme`, `… @ Acme`, `… à Acme`, `… لدى Acme`);
  4. the first Experience entry.

  "Freelance", "Self-employed" or "Student" count as no company.
- **One cache row per company**, found by LinkedIn company handle, then by normalised name. Case, accents and
  legal suffixes don't matter: "ACME S.A." and "Acme" are the same row. Everyone you write to there shares it.
- **Fresh for `RESEARCH_TTL_DAYS`** (30 by default; change it on the Companies page). After that, the next draft
  for someone there searches again.
- **Sourced facts only.** Each fact has a category (product, domain, engineering, news) and an http(s) source
  URL. Facts without a source, with a non-http source, or citing LinkedIn are dropped. If none remain, the
  research has failed.
- **Never from memory.** A research reply counts only if the API reports at least one web search. If it reports
  zero, or reports nothing, the reply is discarded as failed. There is never a fallback call without search.
- **Failures back off**: 1 h after the first failure, 6 h after the second, then 24 h. Meanwhile drafts go out
  with `<company_research>none</company_research>`. **Refresh research** skips the wait.
- **Concurrent drafts for one company share one search.** Requests in the backend wait for the search already
  running, up to `LLM_TIMEOUT_SECONDS` + 30 s. A lease on the database row also stops a second process, or a
  restart, from starting a duplicate.
- **Your edits win.** Research you edited on the dashboard never expires and is never searched again
  automatically, and the extension's Refresh leaves it alone too. Only the dashboard's **Refresh research**,
  after a confirmation, replaces it.

## Web search: what was found

The requirement was to use claude-headless-api's own web search, and never let research quietly turn into the
model answering from memory.

- **claude-headless-api supports it.** A request opts in with `"web_search_options": {}` (OpenAI's field). The
  server then runs `claude -p` with only the `WebSearch` tool, for at most 10 turns.
  - The search runs on Anthropic's side. There is no `WebFetch`, so pages are never opened from your
    machine.
  - An admin has to turn on **Allow web search**. Until then the API answers `400 web_search_disabled`, which
    shows up in the panel as a research failure.
  - When this was written, the support was still uncommitted work in the local claude-headless-api checkout.
    The running container already served it.
- **Its responses didn't say whether a search ran.** The run's search count was computed but dropped from
  `usage`. Without it, "research" that Claude wrote from memory, with plausible URLs, would be
  indistinguishable from real research.
  - **Decision:** claude-headless-api now reports the count in `usage.server_tool_use.web_search_requests`
    (Anthropic's field name). This is a small change in that repo, also uncommitted, with a test and a doc
    line.
  - This backend rejects any research reply that reports zero searches or no count at all, and it never
    retries without search.
- **Structured output works there too.** `response_format: json_schema` maps to `claude -p --json-schema`.
  Research and drafting both send their schema, and the lenient JSON parser stays as a fallback.
- **Rejected alternative:** fetching each cited URL from the backend to check that it exists. It would catch
  invented URLs but not a real homepage cited for a remembered fact, and it would mean outbound requests to
  arbitrary hosts. Facts are still checked: every one needs an http(s) source, and LinkedIn sources are
  dropped.

## Generation contract

The system prompt (Prompts page) is written against a fixed contract. The user message is exactly:

```
<mode>connection_note | message | inmail</mode>
<language>en | fr | ar</language>
<extra_instruction>…or empty</extra_instruction>
<my_context>…active context…</my_context>
<target_profile>…raw profile text…</target_profile>
<company_research>…sourced facts, or "none"…</company_research>
```

`<company_research>` holds one line per fact: `- [category] fact (date) (source: URL)`.

**The reply.** The model replies with JSON. The request also sends this shape as `response_format: json_schema`.

```json
{"strategy": "hook_only | send_cv | ask_referral", "strategy_reason": "…", "cv_match": "…",
 "subject": "… or null", "message": "…", "facts_used": [{"fact": "…", "source": "url | profile | my_context"}]}
```

**What the backend checks:**
- Fences and surrounding text are tolerated.
- An invalid reply gets one repair request. If it is still invalid, the request fails with `llm_bad_json`.
- An unknown strategy is stored as none, with a warning.
- The subject is required for InMail and dropped for other modes.
- Each `facts_used` source is labelled. A URL that isn't one of the research's sources is flagged "not in the
  company research".

**What is stored.** Every field is kept with the generation, together with the company, whether research was
used, and the exact `<company_research>` text sent.

**Character limits** are enforced as before: one shorten retry that asks for the same JSON, shorter.

## Dashboard

Both the dashboard and the side panel have a Light / System / Dark theme switch: in the top bar on the
dashboard, and in the panel's header icon or Settings. The choice is stored per browser; System follows your
OS setting.

- **Prompts:**
  - Edit the system prompt. Saving always creates a new version, and old versions are never modified.
  - Exactly one version is active. You can activate any version, or start editing from any version.
  - Diff any two versions.
  - Pick the model's **effort** (low, medium, high, xhigh, max, or not sent). It applies from the next
    draft or CV import, with no restart. Until you pick one, `LLM_EFFORT` from `.env` is used.
- **My context:** your background and targets: role, stack, dates, location and notes. Each save is a new
  timestamped version. The "What the model sees" preview scrolls on its own when the context is long.
  - **Fill from your CV:** drop a PDF, DOCX, TXT or image (PNG/JPG, e.g. a phone photo) on the import card.
    The backend reads the text, then asks the LLM to turn it into the context fields, and the form is
    pre-filled for you to review. **Nothing is saved until you click Save**, and Undo puts the previous
    values back.
    - A PDF with a text layer is read directly. A scanned PDF (almost no text layer) is rendered page by
      page and OCR'd with Tesseract, up to the first 5 pages. Images are OCR'd directly.
    - OCR languages come from `OCR_LANGS` (default `eng+fra`; `ara` is also installed).
    - The extracted text is shown under the result so you can see what the LLM worked from.
    - Old `.doc` files aren't supported: save as `.docx` or PDF first.
- **People:**
  - Everyone you've sent a message to, most recently contacted first: their headline, current company (a
    link to its research), how many messages you sent, when you last wrote and its status, and how many
    replied.
  - Search by name, headline or company, and optionally include people you only drafted for.
  - **Messages →** opens History filtered to that person.
- **Companies:**
  - Every company researched: status (fresh, expired, failed with the reason and the next retry, edited by
    you), fact count, age, and how many drafts and sent messages it has.
  - **Research** on a row (or **Show all research**) expands that company's facts in place, grouped by category
    with clickable sources.
  - The research expiry setting.
  - **Each company's page:**
    - Its facts, grouped by category, with clickable sources.
    - **Refresh research**. If you edited the research, it asks before discarding your version.
    - An editor to rewrite the research by hand. Every fact still needs a source URL.
- **History:**
  - Filter by mode, language, status, prompt version, strategy, company, person and date range. Unsent drafts are hidden unless you
    ask for them.
  - Each row has Replied / No reply / Reset controls, plus the strategy and company. A company drafted without
    research says so.
- **Stats:**
  - Totals and the rate over the last 7 days.
  - Sent per day and per week, split by mode.
  - Reply rate per prompt version and per mode.
  - Reply rate per strategy, and with vs without company research. These two count only messages drafted
    since strategies existed, and the page says how many older ones are left out.
  - All rate charts count only sent messages, show `n` and a 95% Wilson interval, and fade small samples.
  - Time-to-reply distribution, once there are at least 5 replies.

## Tests

```bash
docker compose run --rm backend pytest
```

The tests can also run locally on Python 3.10+:

```bash
cd backend && pip install -r requirements.txt -r requirements-dev.txt && pytest
```

The tests cover:

- Generation with the LLM mocked:
  - the exact contract message;
  - JSON parsing and the repair retry;
  - every field stored;
  - fact source verification;
  - drafting without research.
- Company research:
  - cache hits per company, expiry and its setting;
  - failure backoff, and `force`;
  - manual edits never overwritten;
  - web search disabled and replies without searches discarded;
  - deduplication of concurrent research, threaded on a real SQLite file, including the database lease;
  - rejection of unsourced and LinkedIn-sourced facts.
- Company detection and name normalisation.
- The People list, the Companies page and its inline research, the strategy, company and person history
  filters, and the strategy and research stats.
- The additive schema migration.
- Limit enforcement and the shorten-retry.
- Prompt and context versioning.
- Status changes and history filters.
- The stats queries.
- The LLM client's error mapping.
- Auth and CORS.

## Decisions

- **Side panel, not a popup.**
  - A popup closes as soon as you click into the LinkedIn page, which would lose your edits. The side panel
    stays open while you paste.
  - This needs a one-line service worker (`setPanelBehavior`), which has no listeners on tabs or the
    network.
- **The active tab's URL decides when the extension is active.**
  - Because host permission covers only `linkedin.com/in/*`, Chrome exposes the tab URL only on profile
    pages, so it needs no `tabs` permission.
  - The panel watches the URL to enable Generate and to warn when the draft is for a different profile.
    It never reads the page outside a click.
- **Raw text, not selectors.**
  - The extractor takes `main`'s `innerText` and drops LinkedIn UI labels (Connect, Show all, …see more,
    …). It removes the back-to-back duplicates that LinkedIn's screen-reader spans create.
  - The name comes from `h1`, falling back to the page title. The headline is the line after the name,
    falling back to `.text-body-medium`.
  - Text is capped at 15,000 characters in the extension and 20,000 in the backend. The LLM interprets
    the rest.
  - The current company is the one exception: it comes from two attributes, the "Current company" label and
    the first Experience entry's company link. A label is more reliable than parsing text, and if it's
    missing the backend falls back to the headline and the text.
- **Profile text and research are untrusted.** They go into `<target_profile>` and `<company_research>`. The
  default system prompt and the research prompt both say to treat them as data and ignore any instructions in
  them.
- **Characters are counted as Unicode code points**: Python `len()` and JS `[...s].length`, so both sides
  agree. This matches what you see for Arabic and French. Emoji count as one character, which the prompt
  forbids anyway.
- **`message` has an 8000 hard limit**, which is LinkedIn's cap, plus a soft target of about 600
  characters stated in the prompt. Your spec gave no limit for that mode, and long DMs to strangers don't
  work.
- **Limits are enforced on the server.**
  - If any field is over, the backend makes one retry. The retry tells the model the exact count and asks
    for at most 90% of the limit.
  - If it is still over, the draft is returned flagged `over_limit`. The panel then refuses **Mark as sent**
    until you trim it.
- **The generation contract replaced the backend's task block.**
  - The user message is now the six fixed tags and nothing else. The system prompt, which you own, explains
    the modes, limits, languages and strategies, and asks for the JSON.
  - The seeded default prompt (`v2 – research + JSON`) is written for it, but it is seeded only into a fresh
    database. An existing database keeps its prompts: activate one written for the contract, because the old
    plain-text v1 makes every draft fail with `llm_bad_json`.
  - The backend still owns the limits: it counts, retries and flags. `modes.py` keeps only keys, labels and
    limits, and the extension's dropdowns still come from `/api/options`.
- **The draft's JSON is parsed leniently, then checked strictly.** Fences and wrapping quotes are stripped, and
  one repair retry covers a reply that isn't JSON. Strategy values, subject rules and fact sources are then
  validated, and problems become warnings in the panel rather than silent fixes.
- **French uses vouvoiement by default. Arabic is MSA**, with technology names kept in Latin script.
- **Regenerate creates a new `generations` row.** Every draft keeps its prompt version for analysis. Unsent
  drafts are hidden from History by default and never count in rates.
- **Reply rate = replied / all sent** (sent + replied + no_reply). Messages still awaiting a reply count
  in the denominator and are shown separately. Bars with n < 10 are faded and tagged "small n", and every
  rate shows its 95% Wilson interval.
- **`replied_at` can be backdated.** You can pick the date in History, and a backdated reply is stored at
  noon local time. A reply can't predate its message: it is clamped to `sent_at`.
- **Re-marking as sent** updates the stored text but keeps the original `sent_at`.
- **Auth is one static token.**
  - The API needs `Authorization: Bearer <APP_TOKEN>`.
  - The dashboard login sets an HttpOnly, SameSite=Strict cookie that holds an HMAC derived from the
    token, never the token itself. Rotating the token logs you out.
  - CSRF is covered by SameSite=Strict for this localhost-only tool.
  - CORS allows `chrome-extension://` origins only.
- **The backend binds to `127.0.0.1:8765` only.**
  - Port 8765 avoids claude-headless-api's default 8000, and 3000.
  - The app refuses to start if `APP_TOKEN` is unset or left as `change-me`.
- **No Alembic, but one additive migration.** Tables are created on startup. Columns that a newer model adds to
  an existing table are then added with `ALTER TABLE ADD COLUMN`: nullable only, so old rows stay valid and
  your volume keeps working. Anything non-additive is still a manual SQL step. Columns added this way carry no
  foreign-key constraint, because SQLite can't add one; fresh databases get it.
- **SQLite runs in WAL mode.** The profile upsert is committed *before* the slow LLM call, so marking
  something sent never waits on a generation in progress.
- **The LLM client does not retry upstream errors.** A single Claude headless call can take a minute.
  Errors are mapped to clear messages instead: unreachable (with the Docker networking hint), bad key,
  forbidden model or effort, rate limited, timeout.
- **Times are stored as naive UTC.** Days and weeks are bucketed in `TIMEZONE`. Weeks start on Monday.
- **The theme is a per-browser preference** stored in localStorage, not on the server. Light and dark are
  one set of color tokens redefined, and the charts redraw from those tokens when you switch.
- **CV import pre-fills the form and never saves.** The LLM can misread a CV, and your context feeds every
  draft, so you check it first. The upload is processed in memory and not stored. Only the context you
  save is kept.
- **The CV prompt extracts, it doesn't write.** It asks for facts from the CV only, in the CV's language,
  and leaves out contact details (email, phone, address), which have no place in a message draft. The CV
  text is treated as data, like profile text.
- **OCR is Tesseract + pdftoppm (poppler) as CLI tools**, not PyMuPDF or a cloud OCR. They are Debian
  packages (no AGPL wheel, nothing leaves your machine), and the CLI avoids extra Python bindings. A PDF
  is OCR'd only when its text layer is under 200 characters, and only the first 5 pages, since a CV
  rarely runs longer and OCR takes a few seconds per page.
- **Fonts are bundled, not loaded from Google Fonts.** Inter for the UI, JetBrains Mono for the prompt
  editor and context preview, and Noto Sans Arabic for Arabic drafts, all SIL OFL. They live in
  `backend/app/static/fonts/` and `extension/fonts/`, so nothing leaves your machine and the extension needs
  no extra permissions. `unicode-range` means the Arabic font is downloaded only when Arabic text is on screen.
- **Effort is a dashboard setting; the model stays in `.env`.** Effort is the knob you tune while
  iterating on prompts (quality against wait time), so it lives in the database next to them and changes
  without a restart. `LLM_EFFORT` is only the starting value. The model name is tied to the API key's
  allow-list, so it stays with the rest of the connection settings. The effort isn't recorded per draft.
- **Research is a separate call before drafting.** The panel calls `/api/research`, then `/api/generate`, so
  it can show the two phases without streaming. `/api/generate` runs the same research step itself, which is a
  cache hit after the first call, so API users get the same behaviour from one call.
- **Research is deduplicated twice.**
  - Inside the backend, the first request for a company runs the search and the others wait for its result
    (one uvicorn worker, so this covers concurrent drafts).
  - A lease on the company row (`status = researching` until `lease_until`) makes a second process or a
    restart skip the search instead of duplicating it. A crashed run's lease lapses on its own.
- **Failed research is cached too, with a backoff** (1 h, 6 h, then 24 h), so a down search or an obscure
  company doesn't add a minute to every draft. A stale failure never puts old facts into a prompt.
- **Your edits are protected even mid-search.** If you save an edit while a search for that company is running,
  the search result is discarded when it arrives.
- **Strategy stats leave out older messages.** Messages sent before this feature have no strategy and no
  research step. Counting them as "without research" would compare prompt versions, not research.
- **No automatic follow-ups or reminders.** That would drift toward automation, and you decide when to
  write again.
