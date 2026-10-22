"""Read recent messages from ONE WhatsApp Web conversation you opened yourself.

How it stays within bounds:
  * You log in (QR code) and open the conversation in the browser window.
  * We only read the message list of the currently open chat, and only if its
    title matches the name you typed. We never touch the chat list.
  * Read-only: nothing is sent, no background loop, runs once per Sync click.

Run by the UI as a subprocess:
    python -m health_tracker.whatsapp.collector --chat "Test Chat" --limit 30
Prints a JSON list to stdout. Progress goes to stderr (never message text).

Debugging selectors (prints page STRUCTURE only, message text is redacted):
    python -m health_tracker.whatsapp.collector --chat "Test Chat" --inspect > skeleton.txt
"""
import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from playwright.sync_api import sync_playwright

from .normalize import merge_best_rows, to_raw_message

ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT / ".browser_profile"  # holds your login session: gitignored
WA_URL = "https://web.whatsapp.com"

# These selectors are the fragile part: WhatsApp can change its page at any
# time. If extraction returns nothing, run --inspect and adjust here only.
TITLE_JS = """() => {
  const el = document.querySelector('#main header span[dir="auto"]');
  return el ? el.innerText : null;
}"""

# rowText is a fallback: the whole row's visible text, parsed in normalize.py
# when the metadata attribute or text span can't be found.
EXTRACT_JS = """() => Array.from(document.querySelectorAll('#main div[data-id]')).map(row => {
  const meta = row.querySelector('[data-pre-plain-text]');
  const textEl = row.querySelector('span.selectable-text');
  return {
    id: row.getAttribute('data-id'),
    prePlain: meta ? meta.getAttribute('data-pre-plain-text') : null,
    text: textEl ? textEl.innerText : '',
    rowText: row.innerText,
    hasImage: !!row.querySelector('img[src^="blob:"]'),
  };
})"""

# Prints each row's element tree: tags, class names, attribute NAMES, and the
# LENGTH of text nodes. Attribute values and text are never included.
SKELETON_JS = """(n) => {
  const SHOW = ['data-testid', 'data-icon', 'role', 'dir', 'class'];
  const walk = (el, depth) => {
    if (depth > 14) return [];
    const attrs = Array.from(el.attributes).map(a =>
      SHOW.includes(a.name) ? `${a.name}=${a.value}` : a.name);
    const texts = Array.from(el.childNodes)
      .filter(c => c.nodeType === 3 && c.textContent.trim())
      .map(c => `<text len=${c.textContent.trim().length}>`);
    const line = '  '.repeat(depth) + el.tagName.toLowerCase() + ' ' +
      attrs.join(' ') + ' ' + texts.join(' ');
    return [line].concat(...Array.from(el.children).map(c => walk(c, depth + 1)));
  };
  const rows = Array.from(document.querySelectorAll('#main div[data-id]')).slice(-n);
  return rows.map((r, i) => `--- row ${i} ---\\n` + walk(r, 0).join('\\n')).join('\\n');
}"""


# Finds the scrollable message pane and moves it. factor > 0 jumps to the
# newest message; factor < 0 scrolls up by that fraction of the visible height.
SCROLL_JS = """(factor) => {
  let el = document.querySelector('#main div[data-id]');
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight) break;
    el = el.parentElement;
  }
  if (!el || el === document.body) return null;
  if (factor > 0) el.scrollTop = el.scrollHeight;
  else el.scrollTop = Math.max(0, el.scrollTop + factor * el.clientHeight);
  return el.scrollTop;
}"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _with_chat(chat_name: str, wait_seconds: int, fn):
    """Open WhatsApp Web, wait until YOUR chosen chat is open, run fn(page)."""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1280, "height": 900}
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(WA_URL)
            log("Log in if asked, then open the conversation yourself.")

            wanted = chat_name.strip().casefold()
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                try:
                    title = page.evaluate(TITLE_JS)
                except Exception:  # page still loading/navigating
                    title = None
                if title and title.strip().casefold() == wanted:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"Timed out: the open conversation never matched {chat_name!r}."
                )

            try:  # wait for typed messages to appear instead of guessing a delay
                page.wait_for_selector("#main [data-pre-plain-text]", timeout=10000)
            except Exception:
                pass  # the chat may have no typed messages; carry on
            page.wait_for_timeout(1000)
            return fn(page)
        finally:
            ctx.close()


def read_rows(page, limit: int, max_steps: int = 12) -> list[dict]:
    """Read the newest `limit` rows, scrolling up until they have all filled in.

    WhatsApp Web only fills in messages near the visible area; the rest are
    empty shells. So: start at the newest message, read, scroll up, read
    again, and keep the best version of every row.
    """
    best: dict = {}
    order: list[str] = []
    page.evaluate(SCROLL_JS, 1)  # jump to the newest message
    prev_top = None
    for step in range(max_steps):
        page.wait_for_timeout(800)
        order = merge_best_rows(best, page.evaluate(EXTRACT_JS))
        wanted = order[-limit:]
        filled = sum(1 for i in wanted if (best[i].get("rowText") or "").strip())
        if filled == len(wanted):
            break
        top = page.evaluate(SCROLL_JS, -0.8)
        if top is None:
            log("Could not find the scrollable message pane.")
            break
        if top == prev_top:  # no movement: we reached the oldest loaded message
            break
        prev_top = top
    log(f"Filled in {filled} of {len(wanted)} rows after {step + 1} read(s).")
    return [best[i] for i in order][-limit:]


def collect(chat_name: str, limit: int = 30, wait_seconds: int = 240) -> list[dict]:
    rows = _with_chat(chat_name, wait_seconds, lambda page: read_rows(page, limit))
    return [asdict(to_raw_message(r)) for r in rows]


def inspect(chat_name: str, rows: int = 8, wait_seconds: int = 240) -> str:
    return _with_chat(chat_name, wait_seconds,
                      lambda page: page.evaluate(SKELETON_JS, rows))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True, help="exact conversation title")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--inspect", action="store_true", help="print redacted page structure")
    ap.add_argument("--rows", type=int, default=8, help="rows to show with --inspect")
    args = ap.parse_args()
    try:
        if args.inspect:
            print(inspect(args.chat, args.rows))
        else:
            print(json.dumps(collect(args.chat, args.limit)))
        return 0
    except Exception as e:
        log(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
