"""browser: open a page in a real (headless) browser and look at it.

For testing web apps: web_fetch only sees the HTML a server sends, not what
the JavaScript renders, and cannot click anything. This tool loads the page in
Chromium (Playwright), optionally runs a few steps (click, fill, press, wait),
and returns the visible text, console errors and failed requests, and the path
of a screenshot.

Each call starts a fresh browser and closes it again (Playwright's objects
belong to the thread that made them, and the TUI runs turns on worker
threads), so a sequence of steps belongs in one call. Opt in with
`"browser": true` — the tool definition is not sent otherwise — and
`pip install playwright && playwright install chromium`, or point it at a
Chrome/Chromium that is already installed:
`"browser": {"executable_path": "/usr/bin/chromium"}`.

Loading a page is like web_fetch and runs without asking; steps that act on
the page (click, fill, press, select) can submit forms, so they ask first.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

from coded.limits import get_limits
from coded.tools.base import Tool, ToolContext, ToolResult

ACTIONS = ("click", "fill", "press", "select", "wait_for", "wait")
_ACTING = ("click", "fill", "press", "select")
INSTALL = "pip install 'coded[browser]' && playwright install chromium"


def _describe(step: Dict[str, Any]) -> str:
    action = step.get("action", "?")
    bits = [action]
    if step.get("selector"):
        bits.append(repr(step["selector"]))
    if step.get("value") is not None and action != "wait":
        bits.append(f"= {step['value']!r}")
    return " ".join(bits)


class BrowserTool(Tool):
    def __init__(self, executable_path: str = "") -> None:
        self.executable_path = executable_path or ""

    name = "browser"
    requires_permission = False  # loading is free; acting steps ask per call (below)
    untrusted_output = True
    brief = ("Open a URL in a headless browser (JS rendered), optionally click/fill/press, "
             "and get the visible text, console errors and a screenshot path.")
    description = """Open a web page in a real headless browser (Chromium) and report \
what a user would see: the rendered visible text, the page title, JavaScript console \
errors, failed network requests and the path of a full-page screenshot. Use it to \
test web apps (e.g. http://localhost:3000) — web_fetch cannot run JavaScript or click.

`steps` run in order after the page loads: click (selector), fill (selector, value), \
press (selector, value = key such as "Enter"), select (selector, value), wait_for \
(selector), wait (value = milliseconds). Selectors are CSS or Playwright selectors \
such as `text=Sign in` or `role=button[name="Save"]`. Each call starts a fresh \
browser, so put a whole interaction in one call. Steps that act on the page ask \
the user first."""
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The page to open (http, https or file)."},
            "steps": {
                "type": "array",
                "description": "Actions to run after loading, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(ACTIONS)},
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
            "screenshot": {"type": "boolean",
                           "description": "Save a full-page screenshot (default true)."},
            "viewport": {"type": "string", "description": "WIDTHxHEIGHT, default 1280x800."},
        },
        "required": ["url"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://", "file://")):
            return ToolResult.error("url must start with http://, https:// or file://.")
        steps: List[Dict[str, Any]] = list(args.get("steps") or [])
        for step in steps:
            if step.get("action") not in ACTIONS:
                return ToolResult.error(f"Unknown step action {step.get('action')!r}; "
                                        f"use one of {', '.join(ACTIONS)}.")
        acting = [s for s in steps if s.get("action") in _ACTING]
        if acting and ctx.permissions is not None:
            from coded.permissions import Decision

            detail = f"{url}\n" + "\n".join(_describe(s) for s in acting)
            decision = ctx.permissions.request(key="browser", title="Act on a web page",
                                               detail=detail, target=url)
            if decision is Decision.DENY:
                return ToolResult.error("The user did not allow acting on this page. Load it "
                                        "without steps, or ask how to proceed.")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return ToolResult.error(
                f"The browser tool needs Playwright, which is not installed. The user can "
                f"add it with: {INSTALL}. Until then use web_fetch for static pages.")

        width, height = 1280, 800
        if args.get("viewport"):
            try:
                width, height = (int(n) for n in str(args["viewport"]).lower().split("x"))
            except ValueError:
                return ToolResult.error("viewport must look like 1280x800.")
        console: List[str] = []
        failed: List[str] = []
        done: List[str] = []
        shot = ""
        try:
            with sync_playwright() as pw:
                launch: Dict[str, Any] = {}
                if self.executable_path:
                    launch["executable_path"] = self.executable_path
                browser = pw.chromium.launch(**launch)
                try:
                    page = browser.new_page(viewport={"width": width, "height": height})
                    page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                            if m.type in ("error", "warning") else None)
                    page.on("pageerror", lambda e: console.append(f"uncaught: {e}"))
                    page.on("requestfailed", lambda r: failed.append(
                        f"{r.method} {r.url} ({r.failure or 'failed'})"))
                    response = page.goto(url, wait_until="load", timeout=30_000)
                    status = response.status if response is not None else None
                    for step in steps:
                        action, sel = step["action"], step.get("selector")
                        value = step.get("value")
                        if action in ("click", "fill", "press", "select", "wait_for") and not sel:
                            return ToolResult.error(f"Step {_describe(step)} needs a selector.")
                        if action == "click":
                            page.click(sel, timeout=10_000)
                        elif action == "fill":
                            page.fill(sel, str(value or ""), timeout=10_000)
                        elif action == "press":
                            page.press(sel, str(value or "Enter"), timeout=10_000)
                        elif action == "select":
                            page.select_option(sel, str(value or ""), timeout=10_000)
                        elif action == "wait_for":
                            page.wait_for_selector(sel, timeout=15_000)
                        elif action == "wait":
                            page.wait_for_timeout(min(int(value or 500), 15_000))
                        done.append(_describe(step))
                    if steps:
                        try:
                            page.wait_for_load_state("networkidle", timeout=5_000)
                        except Exception:  # noqa: BLE001 - a page that keeps polling
                            pass
                    title = page.title()
                    final_url = page.url
                    text = page.inner_text("body") if page.query_selector("body") else ""
                    if args.get("screenshot", True):
                        folder = Path(ctx.cwd) / ".coded" / "screenshots"
                        folder.mkdir(parents=True, exist_ok=True)
                        path = folder / f"page-{int(time.time() * 1000)}.png"
                        page.screenshot(path=str(path), full_page=True)
                        shot = str(path)
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001 - Playwright raises many kinds
            msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                msg = (f"Chromium is not installed for Playwright. The user can run: {INSTALL} "
                       f'(or set "browser": {{"executable_path": "…"}} to an installed Chrome).')
            after = f" after: {', '.join(done)}" if done else ""
            return ToolResult.error(f"Browser failed on {url}{after}: {msg}")

        budget = get_limits(ctx).web_fetch_chars
        body = " ".join(text.split()) if len(text) > budget * 4 else text.strip()
        if len(body) > budget:
            body = body[:budget] + f"\n… ({len(body) - budget:,} more characters)"
        lines = [f"URL: {final_url}" + (f" (HTTP {status})" if status else ""),
                 f"Title: {title}"]
        if done:
            lines.append("Steps run: " + "; ".join(done))
        if shot:
            lines.append(f"Screenshot: {shot}")
        lines.append("Console errors: " + ("none" if not console else ""))
        lines += [f"  {c}" for c in console[:30]]
        if failed:
            lines.append("Failed requests:")
            lines += [f"  {f}" for f in failed[:30]]
        lines += ["", "Visible text:", body or "(none)"]
        return ToolResult.ok("\n".join(lines))
