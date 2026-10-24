# LinkedIn outreach assistant

This is a personal tool for an internship search. On the LinkedIn profile you're viewing, open the side panel
and click **Generate**. You get a short, specific draft written in a real student's voice. You edit it, copy
it, then paste and send it yourself on LinkedIn. Afterwards, **Mark as sent** records the text you actually
sent. A small dashboard is where you iterate on the prompt and see which versions get replies.

```
extension/  Chrome MV3 side panel (plain JS, no build step)
backend/    FastAPI + SQLite: prompts, your context, history, LLM calls, dashboard (Jinja2 + HTMX + Chart.js)
docker-compose.yml, .env.example
```

## What it never does

- **No automation on LinkedIn.** The extension never clicks, types, submits, sends messages or sends
  connection requests. Its only output is your clipboard.
- **Only the current page, only on demand.**
  - There is no content script. The page is read once, through a single read-only
    `chrome.scripting.executeScript` call, and only when you click Generate. Regenerate reuses that same
    text.
  - It never fetches other LinkedIn pages, never calls LinkedIn's internal APIs, and never crawls.
  - Host permission is limited to `https://www.linkedin.com/in/*`.
  - The service worker contains one line, which makes the toolbar icon open the panel.

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

- `APP_TOKEN`: any long random string. Generate one with
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`: your OpenAI-compatible endpoint.

Then start the backend:

```bash
docker compose up -d --build
```

That is the whole backend setup: one container, with SQLite stored on the named volume `data`.

- Dashboard: <http://localhost:8765>. Log in with `APP_TOKEN`.
- API docs: <http://localhost:8765/api/docs>.
- On first start the database is seeded with a default prompt (`v1 – default`) and a placeholder
  context. **Open "My context" and fill it in first.** Until you do, drafts are generic and come with a
  warning.

## Load the extension

1. Go to `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and select the `extension/` folder.
3. Pin the extension and click its icon. The side panel opens.
4. In **Settings** at the bottom of the panel:
   - Set the backend URL (`http://localhost:8765`, or your `PORT`) and the token (`APP_TOKEN`).
   - Click **Test connection**.

Settings are stored in `chrome.storage.local`. The current draft is kept in `chrome.storage.session`, so it
survives closing the panel and is cleared when the browser closes.

### Using it

1. Open someone's profile (`linkedin.com/in/...`) and let it load. Scrolling down first helps, because
   LinkedIn lazy-loads sections such as experience and activity.
2. Pick a mode, a language and, optionally, an extra instruction. Then click **Generate**. Claude headless
   usually takes 10–40 s.

   | Mode | Limit |
   |---|---|
   | `connection_note` | 300 characters |
   | `message` | 8000 hard limit, about 600 targeted |
   | `inmail` | subject 200, body 1900 |

   The languages are English, French and Modern Standard Arabic.
3. Edit the draft. The counters show the mode's limit.
4. **Copy**, paste into LinkedIn, and send it yourself.
5. **Mark as sent**. This stores the edited text, not the original draft.
6. Later, in the dashboard's **History**, mark each message **Replied** (you can set the reply date) or
   **No reply**.

If you have already sent a message to this person, the panel tells you when and what kind.

## Pointing at an LLM API on the host

The backend is the only component that calls the LLM. Calls go to `POST {LLM_BASE_URL}/chat/completions`
with `Authorization: Bearer {LLM_API_KEY}`.

For [claude-headless-api](https://github.com/Ilyass-Bougati/claude-headless-api) running on the same
machine, outside this compose project:

```env
LLM_BASE_URL=http://host.docker.internal:8000/v1
LLM_API_KEY=chk_...        # a token created in claude-headless-api's dashboard
LLM_MODEL=sonnet           # or haiku / opus; GET /v1/models lists what your token may use
LLM_EFFORT=low             # non-standard field that API understands; leave empty for other providers
```

How it fits together:

- `docker-compose.yml` maps `host.docker.internal` to the host through `extra_hosts: host-gateway`. That
  works on Linux with Docker Engine 20.10+ as well as on Docker Desktop.
- The API on the host must listen on an interface the container can reach. If it is itself a container
  that publishes `8000:8000`, it already works. If it binds to `127.0.0.1` only, the container can't
  reach it. In that case, bind it to `0.0.0.0` or to the Docker bridge IP.
- Alternative: put both containers on a shared Docker network and use the service name, for example
  `http://claude-headless-api:8000/v1`.

When the LLM can't be reached, rejects the key, rate limits or times out, the extension shows the backend's
explanation verbatim.

## Dashboard

Both the dashboard and the side panel have a Light / System / Dark theme switch: in the top bar on the
dashboard, and in the panel's header icon or Settings. The choice is stored per browser; System follows your
OS setting.

- **Prompts:**
  - Edit the system prompt. Saving always creates a new version, and old versions are never modified.
  - Exactly one version is active. You can activate any version, or start editing from any version.
  - Diff any two versions.
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
- **History:**
  - Filter by mode, language, status, prompt version and date range. Unsent drafts are hidden unless you
    ask for them.
  - Each row has Replied / No reply / Reset controls.
- **Stats:**
  - Totals and the rate over the last 7 days.
  - Sent per day and per week, split by mode.
  - Reply rate per prompt version and per mode. These count only sent messages, show `n` and a 95% Wilson
    interval, and fade small samples.
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

- Generation, with the LLM mocked.
- Limit enforcement and the shorten-retry.
- Prompt and context versioning.
- Status changes and history filters.
- The stats queries.
- The LLM client's error mapping.
- Auth and CORS.

## Backup

Everything lives in `/data/app.db` on the `data` volume. To back it up:

```bash
docker compose cp backend:/data/app.db ./app-backup.db
```

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
- **Profile text is untrusted.** It goes into the prompt inside `<their_profile>` tags, with an explicit
  instruction to ignore any instructions it contains.
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
- **InMail format is a `Subject:` line, a blank line, then the body**, rather than JSON mode.
  - It works with any OpenAI-compatible server.
  - Localised labels (`Objet:`, `الموضوع:`) and markdown bold are tolerated.
  - If the subject is missing, the draft says so and you write one.
- **The output is cleaned**: code fences, wrapping quotes and a leading "Here's…:" line are stripped.
- **The backend owns the task block; the system prompt is editable.**
  - The editable system prompt holds the voice and rules.
  - The backend appends a fixed task block: your context, their profile, the mode's ask, the language,
    the limits and the output format. Limits and parsing depend on that block, so editing the prompt can't
    break them.
  - The mode and language wording lives in `backend/app/modes.py`. Adding a language is one entry there,
    and the extension's dropdowns come from `/api/options`.
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
- **No Alembic.** Tables are created on startup. For a single-user SQLite tool, a schema change is a
  small manual SQL step or a fresh volume.
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
- **No automatic follow-ups or reminders.** That would drift toward automation, and you decide when to
  write again.
