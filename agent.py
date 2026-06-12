"""
A minimal agentic RPA browser agent.

The core idea is a loop:  OBSERVE -> DECIDE -> ACT -> repeat
  - OBSERVE: read the interactive elements off the page (from the DOM)
  - DECIDE : send the goal + those elements to Gemini, which picks ONE action
  - ACT    : Playwright performs that action in a visible browser
  - repeat until Gemini calls finish()

Run it like:
    ./venv/bin/python agent.py "search trains from Berlin to Munich tomorrow morning"

If you pass no goal, it uses a default bahn.de example.
"""

import os
import sys
import json
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

load_dotenv()

MODEL = os.getenv("AGENT_MODEL", "gemini-2.5-flash")
# If the primary model is overloaded (503), we transparently try the next one.
# These were all confirmed available on this key's free tier.
FALLBACK_MODELS = [MODEL, "gemini-flash-latest", "gemini-2.5-flash-lite"]
# de-duplicate while preserving order, in case MODEL is already in the list
FALLBACK_MODELS = list(dict.fromkeys(FALLBACK_MODELS))
MAX_STEPS = 25  # safety cap so the agent can't loop forever


# ---------------------------------------------------------------------------
# OBSERVE: pull the interesting interactive elements out of the live DOM.
# We label each one with an integer id so the LLM can refer to it simply.
# ---------------------------------------------------------------------------

# This JS runs inside the page. It walks the DOM, keeps only elements a user
# could actually interact with and that are visible, and returns a compact
# description of each (tag, a readable label, and a stable selector).
COLLECT_ELEMENTS_JS = r"""
() => {
  const out = [];
  const selector = 'a, button, input, textarea, select, [role=button], [role=link], [role=combobox]';

  // Clear tags from previous observations so ids never collide across turns.
  for (const el of document.querySelectorAll('[data-agent-id]')) {
    el.removeAttribute('data-agent-id');
  }

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
    if (rect.bottom < 0 || rect.top > (window.innerHeight + 600)) return false;
    return true;
  };

  let i = 0;

  // Walk a root (document or shadowRoot), recursing into shadow DOM and
  // same-origin iframes. Many real sites (bahn.de's cookie consent included)
  // hide their controls inside shadow roots, which a plain querySelectorAll
  // on `document` would never see.
  const walk = (root) => {
    let nodes;
    try { nodes = root.querySelectorAll(selector); } catch (e) { return; }

    for (const el of nodes) {
      if (!isVisible(el)) continue;

      const label = (
        el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') ||
        el.value ||
        el.innerText ||
        el.getAttribute('title') ||
        el.getAttribute('name') ||
        ''
      ).trim().replace(/\s+/g, ' ').slice(0, 100);

      el.setAttribute('data-agent-id', String(i));
      out.push({
        id: i,
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        label: label,
      });
      i++;
    }

    // Recurse into any open shadow roots on this root's elements.
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };

  walk(document);

  // Same-origin iframes (cross-origin ones will throw and are skipped).
  for (const frame of document.querySelectorAll('iframe')) {
    try {
      if (frame.contentDocument) walk(frame.contentDocument);
    } catch (e) { /* cross-origin, skip */ }
  }

  return out;
}
"""


def observe(page):
    """Return a list of interactive elements currently on the page."""
    return page.evaluate(COLLECT_ELEMENTS_JS)


def format_elements(elements):
    """Turn the element list into a compact text block for the LLM prompt."""
    lines = []
    for e in elements:
        t = f" type={e['type']}" if e["type"] else ""
        lines.append(f"[{e['id']}] <{e['tag']}{t}> {e['label']}")
    return "\n".join(lines) if lines else "(no interactive elements found)"


# ---------------------------------------------------------------------------
# ACT: the set of actions the agent is allowed to take. We declare these to
# Gemini as "tools" (function calling), so it can only respond with one of
# these structured actions instead of free-form text.
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="navigate",
            description="Navigate the browser to a URL.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"url": types.Schema(type=types.Type.STRING)},
                required=["url"],
            ),
        ),
        types.FunctionDeclaration(
            name="click",
            description="Click the element with the given id.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"element_id": types.Schema(type=types.Type.INTEGER)},
                required=["element_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="type_text",
            description="Type text into the input element with the given id.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "element_id": types.Schema(type=types.Type.INTEGER),
                    "text": types.Schema(type=types.Type.STRING),
                    "submit": types.Schema(
                        type=types.Type.BOOLEAN,
                        description="Press Enter after typing. Default false.",
                    ),
                },
                required=["element_id", "text"],
            ),
        ),
        types.FunctionDeclaration(
            name="finish",
            description="Call when the goal is achieved. Provide a short summary of the result.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"summary": types.Schema(type=types.Type.STRING)},
                required=["summary"],
            ),
        ),
    ])
]


def element_locator(page, element_id):
    """Find the live element we tagged during observe().

    `.first` guards against the rare case where a tag lingers in a detached
    subtree; the freshly tagged, visible element is the one we want.
    """
    return page.locator(f"[data-agent-id='{element_id}']").first


def execute_action(page, name, args):
    """Perform one action with Playwright. Returns a short result string."""
    if name == "navigate":
        page.goto(args["url"], wait_until="domcontentloaded")
        return f"navigated to {args['url']}"

    if name == "click":
        loc = element_locator(page, args["element_id"])
        loc.click(timeout=3000)
        return f"clicked element {args['element_id']}"

    if name == "type_text":
        loc = element_locator(page, args["element_id"])
        loc.click(timeout=3000)
        loc.fill(args["text"], timeout=3000)
        if args.get("submit"):
            loc.press("Enter")
        return f"typed '{args['text']}' into element {args['element_id']}"

    return f"unknown action: {name}"


# ---------------------------------------------------------------------------
# DECIDE + the loop tying it all together.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an RPA agent that controls a real web browser to accomplish a user's goal.

On each turn you receive the current page URL and a numbered list of the interactive
elements visible on the page. Choose exactly ONE action (a tool call) to make progress.

Guidelines:
- Think step by step, one action at a time. After each action you will see the updated page.
- To start, navigate to a relevant site if the page is blank.
- Only reference element ids that appear in the current list. The list changes after every action.
- Dismiss cookie banners or consent dialogs FIRST if one is present, before anything else.
  Look for buttons labelled like "Accept", "Alle akzeptieren", "Agree", "OK".
- If an action result reports that something "intercepts pointer events", a dialog or
  overlay is blocking the page. Deal with that overlay before retrying.
- Many sites use autocomplete: after typing a station/city into a field, a dropdown of
  suggestions usually appears as new elements. Click the matching suggestion before moving on.
- When the goal is achieved (e.g. results are visible), call finish() with a brief summary.
"""


# Raised when a model's *daily* free-tier allowance is gone. Retrying is
# pointless until the quota window resets, so we surface this distinctly
# instead of pretending the model is merely "busy".
class DailyQuotaExceeded(RuntimeError):
    pass


def classify_429(error):
    """Tell a hard daily-quota 429 apart from a soft per-minute one.

    Gemini returns 429 for both "you've used your 20 requests today" and
    "you're going too fast this minute". They look identical at the status
    level but the body differs: the daily violation carries a quotaId/metric
    mentioning *PerDay* (e.g. GenerateRequestsPerDayPerProjectPerModel), while
    the per-minute one says *PerMinute*. We inspect the text to decide whether
    retrying could ever succeed.

    Returns 'daily', 'per_minute', or 'unknown'.
    """
    text = str(error)
    if "PerDay" in text or "per_day" in text or "RequestsPerDay" in text:
        return "daily"
    if "PerMinute" in text or "per_minute" in text:
        return "per_minute"
    return "unknown"


def pick_available_model(client):
    """Choose a model for the whole run (we must not switch mid-conversation).

    Gemini 2.5 tool calls carry a model-specific thought_signature in the
    history, so a model swap corrupts it. We probe up front and commit to one.

    Preference order:
      1. a model that answers cleanly (200) right now;
      2. failing that, a per-minute rate-limited (429) model — that cap resets
         in seconds and the in-loop retry will ride it out;
      3. if everything is busy (503), give up;
      4. if every model is out of its DAILY quota, say so plainly — waiting a
         minute won't help, so we don't pretend it will.
    """
    from google.genai import errors

    rate_limited = None
    daily_exhausted = []
    for model in FALLBACK_MODELS:
        try:
            client.models.generate_content(model=model, contents="ok")
            return model  # clean 200 — best choice
        except errors.ClientError as e:
            if "429" not in str(e):
                raise
            kind = classify_429(e)
            if kind == "daily":
                print(f"   {model}: daily free-tier quota exhausted.")
                daily_exhausted.append(model)
            else:
                print(f"   {model} is rate-limited (per-minute); "
                      "will use only if nothing better.")
                rate_limited = rate_limited or model
            continue
        except errors.ServerError:
            print(f"   {model} is busy (503), trying another...")
            continue

    if rate_limited:
        print(f"   committing to rate-limited {rate_limited}.")
        return rate_limited
    if daily_exhausted and len(daily_exhausted) == len(FALLBACK_MODELS):
        raise DailyQuotaExceeded(
            "All models have hit the free-tier DAILY request limit (20/day "
            "per model). This resets at midnight Pacific time. To keep going "
            "now, use a different API key/project or enable billing. See "
            "https://ai.google.dev/gemini-api/docs/rate-limits"
        )
    raise RuntimeError("No Gemini model is currently available. Try again shortly.")


def generate_with_retry(client, model, contents, config, max_retries=6):
    """Call one fixed model, retrying the SAME model on transient errors.

    Retries 503 (busy) and per-minute 429 with backoff. A daily-quota 429 is
    NOT retried — it can't succeed until the quota resets — so we raise a clear
    DailyQuotaExceeded immediately rather than spamming "model busy".
    """
    from google.genai import errors

    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except errors.ServerError:  # 5xx, e.g. 503 high demand
            pass
        except errors.ClientError as e:  # 4xx; only 429 is worth retrying
            if "429" not in str(e):
                raise
            if classify_429(e) == "daily":
                raise DailyQuotaExceeded(
                    f"{model}: free-tier DAILY request limit reached (20/day). "
                    "Retrying won't help until it resets at midnight Pacific. "
                    "Use a different API key/project or enable billing to "
                    "continue now."
                ) from e
        if attempt == max_retries:
            raise RuntimeError(f"Gemini unavailable after {max_retries} attempts")
        print(f"           (model busy, retrying in {delay}s...)")
        time.sleep(delay)
        delay = min(delay * 2, 30)


def pursue_goal(client, model, page, contents, config):
    """Run the observe->decide->act loop until the model calls finish().

    Reused for each goal in an interactive session. `page` and `contents` are
    passed in so the browser state and conversation memory carry over.
    """
    for step in range(1, MAX_STEPS + 1):
        try:
            response = generate_with_retry(client, model, contents, config)
        except DailyQuotaExceeded:
            raise  # unrecoverable today: let it end the whole session
        except RuntimeError as e:
            print(f"\n⚠️  {e}. Stopping this goal.")
            return

        candidate = response.candidates[0]
        contents.append(candidate.content)  # remember what the model said

        # Find the tool call in the response.
        call = None
        for part in candidate.content.parts:
            if part.function_call:
                call = part.function_call
                break

        if call is None:
            # Model replied with plain text instead of an action; nudge it.
            text = candidate.content.parts[0].text if candidate.content.parts else ""
            print(f"[step {step}] model said (no action): {text}")
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text="Please respond with a tool call (an action).")],
            ))
            continue

        name = call.name
        args = dict(call.args)
        print(f"[step {step}] action: {name}({args})")

        if name == "finish":
            print(f"\n✅ DONE: {args.get('summary', '')}")
            return

        # ACT
        try:
            result = execute_action(page, name, args)
        except Exception as e:
            # Keep only the first, useful line of Playwright errors.
            result = f"ERROR performing {name}: {str(e).splitlines()[0]}"
        print(f"           -> {result}")

        page.wait_for_timeout(1200)  # let the page settle

        # OBSERVE the new state and feed it back as the tool result.
        elements = observe(page)
        observation = (
            f"Action result: {result}\n"
            f"Current URL: {page.url}\n"
            f"Interactive elements now on the page:\n{format_elements(elements)}"
        )
        contents.append(types.Content(
            role="user",
            parts=[types.Part.from_function_response(
                name=name, response={"observation": observation}
            )],
        ))

    print("\n⚠️  Reached step limit without finishing.")


def run_agent(first_goal=None, interactive=False):
    """Set up the browser once, then pursue one goal (or chat for many).

    - one-shot:    run_agent("some goal")
    - interactive: run_agent(interactive=True)  -> asks for goals in a loop,
                   keeping the same browser and memory between them.
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model = pick_available_model(client)
    print(f"Using model: {model}\n")

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=TOOLS,
        temperature=0,
        # Disable "thinking": this simple one-action-per-turn loop doesn't
        # need it, it's faster/cheaper, and crucially it avoids the
        # thought_signature requirement that breaks model fallback.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    with sync_playwright() as p:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.set_viewport_size({"width": 1280, "height": 900})

            # One conversation history, reused across goals so the agent
            # remembers everything it has already done on the page.
            contents = []

            def add_goal(goal):
                where = "The browser is currently blank." if not page.url or page.url == "about:blank" \
                    else f"The browser is currently on {page.url}."
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=f"GOAL: {goal}\n\n{where} Begin.")],
                ))

            if not interactive:
                add_goal(first_goal)
                pursue_goal(client, model, page, contents, config)
                time.sleep(3)  # let you see the final page
                return

            # Interactive chat: keep asking for goals until the user quits.
            print("Interactive mode. Type a goal and watch the browser. "
                  "Type 'quit' (or just Enter) to exit.\n")
            if first_goal:
                add_goal(first_goal)
                try:
                    pursue_goal(client, model, page, contents, config)
                except DailyQuotaExceeded as e:
                    print(f"\n⛔ {e}")
                    return

            while True:
                try:
                    goal = input("\nyou> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if goal.lower() in ("quit", "exit", "q", ""):
                    break
                add_goal(goal)
                try:
                    pursue_goal(client, model, page, contents, config)
                except DailyQuotaExceeded as e:
                    print(f"\n⛔ {e}")
                    break
        finally:
            browser.close()


if __name__ == "__main__":
    args = sys.argv[1:]

    # --chat / -i forces interactive mode. Any remaining args are an optional
    # first goal. With no args at all, default to interactive.
    interactive = False
    if "--chat" in args:
        interactive = True
        args.remove("--chat")
    if "-i" in args:
        interactive = True
        args.remove("-i")

    goal = " ".join(args) if args else None

    if goal is None and not interactive:
        interactive = True  # bare `python agent.py` -> chat

    try:
        if interactive:
            run_agent(first_goal=goal, interactive=True)
        else:
            print(f"GOAL: {goal}\n")
            run_agent(first_goal=goal)
    except DailyQuotaExceeded as e:
        print(f"\n⛔ {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"⚠️  {e}")
        sys.exit(1)
