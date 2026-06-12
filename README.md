# Agentic RPA browser agent

A small, readable LLM-driven browser agent for learning **agentic RPA**. It
drives a Chromium browser (visible locally, headless on a server) to accomplish
a goal you describe in plain language. Everything lives in one file, `agent.py`,
built around the core loop:

```
goal -> OBSERVE (read interactive DOM elements)
     -> DECIDE  (Gemini picks ONE action via function-calling)
     -> ACT     (Playwright performs it)
     -> repeat until the model calls finish()
```

That observe -> decide -> act loop *is* the heart of agentic RPA. The code is
written to be read top to bottom.

---

## What's in here

| File | Purpose |
|------|---------|
| `agent.py` | The whole agent: DOM observation, action tools, the loop. |
| `.env` | Your `GEMINI_API_KEY`, `HEADLESS` flag, etc. (git-ignored, never committed). |
| `requirements.txt` | Python dependencies. |
| `terraform/` | Infrastructure-as-code for the EC2 deployment. |
| `.gitignore` | Keeps secrets and venv out of git. |

---

## One-time setup

Already done in this workspace. For a fresh machine:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m playwright install chromium
```

Put your free Google Gemini API key in `.env`:

```
GEMINI_API_KEY=your-key-here
HEADLESS=false
```

| Variable | Values | Description |
|----------|--------|-------------|
| `GEMINI_API_KEY` | your key | Required. Get one at https://aistudio.google.com/apikey |
| `HEADLESS` | `true` / `false` | `false` opens a visible browser (local dev), `true` runs headless (server). Defaults to `true`. |
| `AGENT_MODEL` | model name | Optional. Defaults to `gemini-2.5-flash`. |

---

## Running it from the IDE (Kiro)

Just run the script in the integrated terminal. A real Chromium window opens so
you can watch the agent work.

Run one goal and exit:

```bash
./venv/bin/python agent.py "go to wikipedia.org and search for Playwright, open the article"
```

Or start an interactive chat session (no goal argument):

```bash
./venv/bin/python agent.py
```

Any custom goal — pass it as an argument:

```bash
./venv/bin/python agent.py "go to wikipedia.org and search for Playwright, open the article"
```

```bash
./venv/bin/python agent.py "Go to bahn.de, accept cookies, search trains from Berlin Hbf to Muenchen Hbf, stop when results show"
```

You'll see a step-by-step log of every action the agent takes.

### Interactive mode (chat with it)

Run with `--chat` (or just run it with no goal) to keep the browser open and
give follow-up instructions. The agent **remembers** what it already did and the
page stays where it is between goals:

```bash
./venv/bin/python agent.py --chat
```

```
you> go to bahn.de and search trains from Berlin Hbf to Muenchen Hbf
... agent works ...
you> now pick the earliest connection
... agent continues on the same page ...
you> quit
```

You can also seed the first goal and then keep chatting:

```bash
./venv/bin/python agent.py --chat "go to wikipedia.org and search for Playwright"
```

Type `quit`, `exit`, or just press Enter on an empty line to leave. Because each
goal adds more API calls, interactive sessions hit the free-tier limits sooner —
wait a minute if it starts reporting "model busy".

### Optional: pick a specific model

By default the agent auto-selects an available model. To force one:

```bash
AGENT_MODEL=gemini-2.5-flash ./venv/bin/python agent.py "your goal"
```

---

## Running it from the Kiro CLI

The agent is just a normal Python program, so the Kiro CLI (a coding/ops agent
in your terminal) can run it for you as a shell command. Two ways:

### 1. Ask the CLI to run it (simplest)

Start a Kiro CLI session in this folder and just ask:

```
> run the browser agent to search trains from Berlin to Munich on bahn.de
```

Kiro will execute the equivalent of:

```bash
./venv/bin/python agent.py "search trains from Berlin Hbf to Muenchen Hbf on bahn.de"
```

Because the browser launches in headed mode, you'll see it work.

### 2. Run it directly in the terminal

You don't even need a chat session — it's a plain command:

```bash
cd /Users/ViktorReif/root/kiro_codes/playwright_showcase/playwright_showcase
./venv/bin/python agent.py "your goal here"
```

> Note: this runs *our* Python agent. It is separate from Kiro's own models —
> it uses your Gemini key. Kiro is just launching the program.

### 3. (Later) Make it a first-class CLI tool via MCP

To get the "summon it natively from a custom agent" experience you saw in the
demo, the next step is to wrap `agent.py` as an **MCP server** exposing a tool
like `search_trains(from, to, date)`, then register that server in a Kiro CLI
custom agent config. Not built yet — this is the planned next layer.

---

## How it works (the parts worth reading)

- **`COLLECT_ELEMENTS_JS` + `observe()`** — runs JS in the page to collect the
  visible interactive elements (links, buttons, inputs), labels each with an id,
  and **pierces shadow DOM and same-origin iframes**. Real sites like bahn.de
  hide their cookie dialog inside shadow DOM, so this matters. Old id tags are
  cleared each turn so ids never collide.
- **`TOOLS`** — the only actions the agent may take: `navigate`, `click`,
  `type_text`, `finish`. Declared to Gemini as function-calling tools, so the
  model returns a structured action instead of free-form text.
- **`execute_action()`** — performs the chosen action with Playwright. Clicks
  fail fast (3s) so a blocking overlay is reported back to the model instead of
  hanging.
- **`pick_available_model()`** — probes once at startup and commits to a single
  model for the whole run. (Switching models mid-conversation breaks Gemini 2.5
  tool-calling, because the history carries model-specific data.)
- **`generate_with_retry()`** — retries the chosen model on transient 503/429
  errors with exponential backoff.
- **`run_agent()`** — the observe/decide/act loop, with the browser always
  closed via `finally`.

---

## Good to know / gotchas

- **Free-tier limits.** The Gemini free tier has per-minute and per-day request
  caps. Rapid repeated runs trigger `429`/`503`. If the agent says "model busy"
  a lot, wait a minute and retry.
- **`gemini-2.0-flash` is not on the free tier** for this project — don't switch
  to it. `gemini-2.5-flash`, `gemini-flash-latest`, and `gemini-2.5-flash-lite`
  all work; the lite model is weaker and occasionally fumbles a step.
- **Thinking is disabled** (`thinking_budget=0`) on purpose: this simple
  one-action-per-turn loop doesn't need it, it's faster/cheaper, and it keeps
  tool-calling history clean.
- **`MAX_STEPS`** caps how many actions the agent can take so it can't loop
  forever.
- Real sites change. If a run stalls, watch the step log to see what the agent
  saw and where it got stuck — that's the useful signal for improving prompts or
  observation.
