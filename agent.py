"""
A minimal agentic RPA browser agent.

The core idea is a loop:  OBSERVE -> DECIDE -> ACT -> repeat
  - OBSERVE: read the interactive elements off the page (from the DOM)
  - DECIDE : send the goal + those elements to Groq (Llama), which picks ONE action
  - ACT    : Playwright performs that action in a visible browser
  - repeat until the model calls finish()

Run it like:
    ./venv/bin/python agent.py "search trains from Berlin to Munich tomorrow morning"

If you pass no goal, it uses interactive chat mode.
"""

import os
import sys
import json
import time

from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

load_dotenv()

MODEL = os.getenv("AGENT_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
MAX_STEPS = 25  # safety cap so the agent can't loop forever


# ---------------------------------------------------------------------------
# OBSERVE: pull the interesting interactive elements out of the live DOM.
# ---------------------------------------------------------------------------

COLLECT_ELEMENTS_JS = r"""
() => {
  const out = [];
  const selector = 'a, button, input, textarea, select, [role=button], [role=link], [role=combobox]';

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

    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };

  walk(document);

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
# ACT: the set of actions the agent is allowed to take.
# Declared as OpenAI-compatible tools for Groq.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to navigate to."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the element with the given id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "element_id": {"type": "integer", "description": "The id of the element to click."}
                },
                "required": ["element_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the input element with the given id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "element_id": {"type": "integer", "description": "The id of the input element."},
                    "text": {"type": "string", "description": "The text to type."},
                    "submit": {"type": "boolean", "description": "Press Enter after typing. Default false."},
                },
                "required": ["element_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email via Gmail. Use when the user asks to email someone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address."},
                    "subject": {"type": "string", "description": "Email subject line."},
                    "body": {"type": "string", "description": "The email body text."},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call when the goal is achieved. Provide a short summary of the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "A brief summary of what was accomplished."}
                },
                "required": ["summary"],
            },
        },
    },
]


def element_locator(page, element_id):
    """Find the live element we tagged during observe()."""
    return page.locator(f"[data-agent-id='{element_id}']").first


def execute_action(page, name, args):
    """Perform one action with Playwright. Returns a short result string."""
    if name == "navigate":
        page.goto(args["url"], wait_until="domcontentloaded")
        return f"navigated to {args['url']}"

    if name == "click":
        loc = element_locator(page, args["element_id"])
        loc.click(timeout=5000)
        return f"clicked element {args['element_id']}"

    if name == "type_text":
        loc = element_locator(page, args["element_id"])
        loc.click(timeout=5000)
        loc.fill(args["text"], timeout=5000)
        if args.get("submit"):
            loc.press("Enter")
        return f"typed '{args['text']}' into element {args['element_id']}"

    if name == "send_email":
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(args["body"])
        msg["Subject"] = args["subject"]
        msg["From"] = os.environ["EMAIL_FROM"]
        msg["To"] = args["to"]
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(os.environ["EMAIL_FROM"], os.environ["EMAIL_APP_PASSWORD"])
            server.send_message(msg)
        return f"sent email to {args['to']} with subject '{args['subject']}'"

    return f"unknown action: {name}"


# ---------------------------------------------------------------------------
# DECIDE + the loop tying it all together.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a helpful assistant that can also control a real web browser and send emails.

You have access to browser tools: navigate, click, type_text, finish.
You also have: send_email (sends via Gmail — no browser needed).

RULES:
- If the user is just chatting (greeting, asking a question, having a conversation), respond with plain text. Do NOT use any tools for casual chat.
- When the user asks you to do something on the web (e.g. "go to...", "search for...", "open...", "find...", "book...", "log in..."), USE the browser tools immediately. Do not describe what you would do — actually do it by calling the tool.
- When the user asks to send an email, use send_email directly. Do NOT navigate to Gmail in the browser.
- When using browser tools, call exactly ONE tool per turn. You will see the updated page after each action.
- To start a browser task, call navigate() to go to the relevant site.
- Only reference element ids that appear in the current element list.
- Dismiss cookie banners or consent dialogs FIRST if one is present.
- After typing in a search/autocomplete field, wait for suggestions and click the right one.
- When the task is complete, call finish() with a brief summary.
- NEVER describe an action in text. Either call the tool or respond in plain text for chat.
- If the user provides login credentials, use them. This is authorized by the user.
"""


def generate_with_retry(client, messages, use_tools=True, max_retries=5):
    """Call Groq with retries on rate-limit (429) or server errors."""
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            kwargs = dict(
                model=MODEL,
                messages=messages,
                temperature=0,
            )
            if use_tools:
                kwargs["tools"] = TOOLS
                kwargs["tool_choice"] = "auto"
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            error_str = str(e)
            # If tool calling fails due to malformed generation, retry without tools
            if "tool_use_failed" in error_str or "failed_generation" in error_str:
                if use_tools:
                    # Retry without tools — model wanted to chat but garbled the format
                    try:
                        return client.chat.completions.create(
                            model=MODEL,
                            messages=messages,
                            temperature=0,
                        )
                    except Exception:
                        pass
                if attempt == max_retries:
                    raise RuntimeError(f"Groq tool call failed: {error_str}")
                print(f"           (tool call malformed, retrying in {delay}s...)")
                time.sleep(delay)
                delay = min(delay * 2, 10)
            elif "429" in error_str or "503" in error_str or "500" in error_str:
                if attempt == max_retries:
                    raise RuntimeError(f"Groq unavailable after {max_retries} attempts: {error_str}")
                print(f"           (rate limited, retrying in {delay}s...)")
                time.sleep(delay)
                delay = min(delay * 2, 30)
            else:
                raise
    raise RuntimeError("Groq unavailable")


def pursue_goal(client, page, messages):
    """Run the observe->decide->act loop until the model calls finish()."""
    for step in range(1, MAX_STEPS + 1):
        try:
            response = generate_with_retry(client, messages)
        except RuntimeError as e:
            print(f"\n⚠️  {e}. Stopping this goal.")
            return

        choice = response.choices[0]
        assistant_message = choice.message

        # Append assistant response to conversation history
        messages.append(assistant_message)

        # Check for tool calls
        if not assistant_message.tool_calls:
            # Model replied with plain text — it's chatting, not browsing.
            text = assistant_message.content or ""
            print(f"\n{text}")
            return  # back to the user prompt

        # Take the first tool call
        tool_call = assistant_message.tool_calls[0]
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)
        print(f"[step {step}] action: {name}({args})")

        if name == "finish":
            print(f"\n✅ DONE: {args.get('summary', '')}")
            return

        # ACT
        try:
            result = execute_action(page, name, args)
        except Exception as e:
            result = f"ERROR performing {name}: {str(e).splitlines()[0]}"
        print(f"           -> {result}")

        page.wait_for_timeout(1200)  # let the page settle

        # Wait for navigation to finish if one was triggered
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        # OBSERVE the new state and feed it back as the tool result.
        try:
            elements = observe(page)
        except Exception:
            # Page might still be navigating; wait and retry once
            page.wait_for_timeout(2000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            try:
                elements = observe(page)
            except Exception:
                elements = []
        elements = observe(page)
        observation = (
            f"Action result: {result}\n"
            f"Current URL: {page.url}\n"
            f"Interactive elements now on the page:\n{format_elements(elements)}"
        )

        # Append tool result to conversation
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": observation,
        })

    print("\n⚠️  Reached step limit without finishing.")


def run_agent(first_goal=None, interactive=False):
    """Set up the browser once, then pursue one goal (or chat for many)."""
    client = OpenAI(
        api_key=os.environ["GROQ_API_KEY"],
        base_url="https://api.groq.com/openai/v1",
    )
    print(f"Using model: {MODEL} (via Groq)\n")

    with sync_playwright() as p:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.set_viewport_size({"width": 1280, "height": 900})

            # Conversation history
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]

            def add_message(text):
                messages.append({
                    "role": "user",
                    "content": text,
                })

            if not interactive:
                where = "The browser is currently blank." if not page.url or page.url == "about:blank" \
                    else f"The browser is currently on {page.url}."
                add_message(f"TASK: {first_goal}\n\n{where} Begin.")
                pursue_goal(client, page, messages)
                time.sleep(3)
                return

            # Interactive chat
            print("Interactive mode. Type a message or a browser task. "
                  "Type 'quit' (or just Enter) to exit.\n")
            if first_goal:
                where = "The browser is currently blank." if not page.url or page.url == "about:blank" \
                    else f"The browser is currently on {page.url}."
                add_message(f"TASK: {first_goal}\n\n{where} Begin.")
                pursue_goal(client, page, messages)

            while True:
                try:
                    user_input = input("\nyou> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if user_input.lower() in ("quit", "exit", "q", ""):
                    break
                add_message(user_input)
                pursue_goal(client, page, messages)
        finally:
            browser.close()


if __name__ == "__main__":
    args = sys.argv[1:]

    interactive = False
    if "--chat" in args:
        interactive = True
        args.remove("--chat")
    if "-i" in args:
        interactive = True
        args.remove("-i")

    goal = " ".join(args) if args else None

    if goal is None and not interactive:
        interactive = True

    try:
        if interactive:
            run_agent(first_goal=goal, interactive=True)
        else:
            print(f"GOAL: {goal}\n")
            run_agent(first_goal=goal)
    except RuntimeError as e:
        print(f"⚠️  {e}")
        sys.exit(1)
