# LinkedIn outreach assistant

A Chrome side panel that drafts a short, personal LinkedIn message for the profile you're looking at. It reads
the profile, looks up what the person's company does, and writes a draft that links something from your CV to
that company. You edit the draft, copy it, and send it yourself. A small dashboard tracks which messages got
replies, so you can improve your prompt over time.

The extension never clicks, types or sends anything on LinkedIn. All it produces is text on your clipboard.

This tutorial takes about 15 minutes, from a fresh clone to your first draft.

## What you need

- **Docker** with Docker Compose, to run the backend.
- **Chrome** (or another Chromium browser), version 116 or newer.
- **An OpenAI-compatible LLM API.** The tool is built for
  [claude-headless-api](https://github.com/Ilyass-Bougati/claude-headless-api). Company research needs its
  web search, which another provider may not offer. Without web search you still get drafts, just without
  company research.

## Step 1: Configure the backend

Copy the example config:

```bash
cp .env.example .env
```

Open `.env` and set these values:

| Variable | What to put |
|---|---|
| `APP_TOKEN` | A long random string. It is your password for the dashboard and the extension. |
| `LLM_BASE_URL` | Your LLM API, ending in `/v1`. |
| `LLM_API_KEY` | The API key for that LLM. |
| `LLM_MODEL` | The model name, for example `sonnet`. |
| `TIMEZONE` | Your timezone, for example `Europe/Paris`. The stats use it. |

To generate a token:

```bash
openssl rand -hex 32
```

If claude-headless-api runs on the same machine, the defaults in `.env.example` already point at it
(`http://host.docker.internal:8000/v1`). Create a token in its dashboard, put it in `LLM_API_KEY`, and tick
**Allow web search** in its **Settings** so company research works.

The other variables are optional. `.env.example` explains each one.

## Step 2: Start the backend

```bash
docker compose up -d --build
```

This runs one container and keeps its database on a Docker volume called `data`. Open
<http://localhost:8765> and log in with your `APP_TOKEN`.

If the page doesn't load, see [Troubleshooting](#troubleshooting).

## Step 3: Tell it about yourself

Drafts are only as good as what the model knows about you. Until you do this step, every draft is generic and
comes with a warning.

1. In the dashboard, open **My context**.
2. Under **Fill from your CV**, drop your CV: a PDF, DOCX, TXT or a photo of it. The form fills itself in.
3. Check every field and fix what the import got wrong: your role, stack, dates, location and the kind of
   internship or job you want. Nothing is saved yet.
4. Click **Save**.

The **What the model sees** box shows exactly what is sent with every draft.

## Step 4: Install the extension

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and select the `extension/` folder of this repository.
3. Pin the extension to the toolbar, then click its icon. The side panel opens.
4. At the bottom of the panel, open **Settings**:
   - **Backend URL**: `http://localhost:8765` (or the `PORT` you set).
   - **Token**: your `APP_TOKEN`.
5. Click **Test connection**. It saves the settings and should answer "Connected", along with your model's
   name.

## Step 5: Write your first message

1. Open someone's profile on LinkedIn (a `linkedin.com/in/...` page). Scroll to the bottom once, so LinkedIn
   loads the Experience and Activity sections.
2. In the side panel, choose:
   - **Message type**:

     | Type | When to use it | Limit |
     |---|---|---|
     | Connection note | With a connection request | 300 characters |
     | Message | You're already connected | about 600 characters |
     | InMail | Premium message to someone outside your network | subject 200, body 1900 |

   - **Language**: English, French or Arabic.
   - **Extra instruction** (optional), for example "mention that I'm available from June".
3. Click **Generate draft**. The button shows two steps:
   - **Researching…**: a web search about the person's company. It takes 30 to 90 seconds the first time,
     and is instant for a company you've already researched.
   - **Writing…**: the draft itself, usually 10 to 40 seconds.
4. Read **Why this draft** before you edit. It shows the strategy the model chose (just a hook, offering your
   CV, or asking for a referral), how your CV matches, and every fact it used with its source. Check the facts:
   a wrong fact in a message to a stranger costs more than a vague one.
5. If the company is wrong, type the right one under **Wrong company?** and click **Regenerate**.
6. Edit the draft until it sounds like you. The counter shows how close you are to the limit.
7. Click **Copy**, paste the text into LinkedIn, and send it yourself.
8. Back in the panel, click **Mark as sent**. This saves the text you actually sent, edits included.

If you've already written to this person, the panel tells you when and how.

## Step 6: Record replies

A few days later, open **History** in the dashboard and mark each message **Replied** (you can pick the date
the reply came) or **No reply**. Without this the stats stay empty.

## Step 7: Improve your prompt

Once you have some replies, the dashboard helps you see what works:

- **Stats** shows your reply rate by prompt version, message type and strategy, and with or without company
  research.
- **Prompts** is where you edit the system prompt that writes the drafts. Each save creates a new version, so
  you can compare versions in Stats and go back to an older one at any time. You can also choose the model's
  **effort** here: higher effort writes better drafts but takes longer.
- **People** lists everyone you've written to, and **Companies** shows the research for each company. You
  can refresh it, or correct it by hand.

Change one thing at a time and wait for at least ten sent messages before comparing: small numbers are
mostly noise, and the charts fade them out for that reason.

## Troubleshooting

**The backend won't start.** Check the logs with `docker compose logs backend`. The most common cause is
`APP_TOKEN` still set to `change-me`.

**Test connection fails.** Check that the container is running (`docker compose ps`), that the URL has no
trailing path, and that the token matches `APP_TOKEN` exactly.

**"LLM unreachable".** The backend runs in Docker, so `localhost` there means the container, not your
machine. Use `host.docker.internal` in `LLM_BASE_URL`, and make sure the LLM API listens on `0.0.0.0` rather
than only `127.0.0.1`. You can also put both containers on one Docker network and use the service name, for
example `http://claude-headless-api:8000/v1`.

**Research fails with "web search turned off".** Turn on **Allow web search** in claude-headless-api's
Settings. The draft is still written, just without company research.

**Generate draft is greyed out.** You're not on a profile page. The extension only works on
`linkedin.com/in/...`.

**The draft ignores parts of the profile.** LinkedIn loads sections as you scroll. Scroll to the bottom of the
profile, then click **Regenerate**.

**Every draft fails with `llm_bad_json`.** The active prompt was written for an older version of the tool.
On the **Prompts** page, activate `v2 – research + JSON` or a version based on it.

**Mark as sent is disabled.** The draft is over the limit for its message type. Shorten it until the counter
is back under the limit.

## Good to know

- **Your data stays on your machine.** The backend only listens on `127.0.0.1`. The only things that leave
  your machine are the calls to your LLM API.
- **Nothing calls linkedin.com.** The extension reads the profile once, when you click Generate draft. It
  never opens other pages, and company research never uses LinkedIn as a source.
- **Back up your history** with:

  ```bash
  docker compose cp backend:/data/app.db ./app-backup.db
  ```

- **Run the tests** with:

  ```bash
  docker compose run --rm backend pytest
  ```

For how research and drafting work in detail, and why things are built the way they are, see
[docs/how-it-works.md](docs/how-it-works.md).
