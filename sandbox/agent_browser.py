#!/usr/bin/env python3
"""Browser helper that runs INSIDE an agent's sandbox.

  agent_browser.py call   < request.json   -> prints one JSON line
  agent_browser.py daemon                  -> keeps one persistent Chromium context alive

The daemon is started on first use and exits after 5 idle minutes to give RAM back. The browser
profile lives in /workspace/.browser so cookies and logins persist between sessions. Pages are
returned as text with interactive elements labelled [n] so the model can click/type by number.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.environ.get("TROUPE_BROWSER_PORT", "9333"))
HOME = Path(os.environ.get("HOME", "/workspace"))
UA = os.environ.get("TROUPE_UA", "TroupeAgent/0.1 (automated)")
BLOCKED = [d for d in os.environ.get("TROUPE_BLOCKED_DOMAINS", "").split(",") if d]
IDLE_SECONDS = 300
MAX_TEXT = 15_000

LABEL_JS = r"""
() => {
  document.querySelectorAll('[data-troupe-ref]').forEach(e => e.removeAttribute('data-troupe-ref'));
  const sel = 'a[href], button, input, textarea, select, [role=button], [role=link], [role=checkbox], [role=tab], [contenteditable=true], summary';
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden' || st.display === 'none') continue;
    n += 1; el.setAttribute('data-troupe-ref', String(n));
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || '';
    let label = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') ||
                 el.getAttribute('title') || el.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    if (type === 'password') label = '(password field - use type_secret)';
    const href = tag === 'a' ? ' -> ' + (el.getAttribute('href') || '').slice(0, 100) : '';
    out.push(`[${n}] ${tag}${type ? '[' + type + ']' : ''} "${label}"${href}`);
    if (n >= 150) break;
  }
  return out.join('\n');
}
"""


def blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in BLOCKED)


class Browser:
    def __init__(self):
        from playwright.sync_api import sync_playwright

        self.pw = sync_playwright().start()
        profile = HOME / ".browser"
        profile.mkdir(parents=True, exist_ok=True)
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(profile), headless=True, user_agent=UA, viewport={"width": 1280, "height": 900},
            args=["--disable-dev-shm-usage", "--no-first-run", "--disable-gpu"])
        self.ctx.set_extra_http_headers({"X-Automated-Agent": "TroupeAgent"})
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        p = self.page
        try:
            p.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        elements = p.evaluate(LABEL_JS)
        text = p.evaluate("() => document.body ? document.body.innerText : ''") or ""
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n...[page text truncated; scroll or use fetch]"
        return {"ok": True, "url": p.url, "title": p.title(),
                "text": f"{text}\n\n--- Interactive elements ---\n{elements}"}

    def el(self, ref):
        return self.page.locator(f'[data-troupe-ref="{int(ref)}"]').first

    def handle(self, req: dict) -> dict:
        a = req.get("action")
        p = self.page
        if a == "goto":
            url = req["url"]
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            if blocked(url):
                return {"ok": False, "error": "domain is blocked by the owner's policy"}
            p.goto(url, timeout=45_000, wait_until="domcontentloaded")
            return self.snapshot()
        if a == "read":
            return self.snapshot()
        if a == "click":
            self.el(req["ref"]).click(timeout=10_000)
            p.wait_for_timeout(800)
            return self.snapshot()
        if a == "type":
            el = self.el(req["ref"])
            is_pw = (el.get_attribute("type") or "").lower() == "password"
            if is_pw and not req.get("secret"):
                return {"ok": False, "error": "password fields only accept type_secret"}
            if req.get("secret"):
                host = (urlparse(p.url).hostname or "").lower()
                if not any(host == d or host.endswith("." + d) for d in req.get("allowed_domains", [])):
                    return {"ok": False, "error": f"this secret is not allowed on {host}"}
            el.fill(req.get("text", ""), timeout=10_000)
            if req.get("submit"):
                el.press("Enter")
                p.wait_for_timeout(1200)
            if req.get("secret"):
                return {"ok": True, "url": p.url}
            return self.snapshot()
        if a == "select":
            self.el(req["ref"]).select_option(label=req.get("text", ""), timeout=10_000)
            return self.snapshot()
        if a == "scroll":
            p.mouse.wheel(0, 800)
            p.wait_for_timeout(400)
            return self.snapshot()
        if a == "back":
            p.go_back(timeout=20_000)
            return self.snapshot()
        if a == "screenshot":
            d = HOME / "screenshots"
            d.mkdir(exist_ok=True)
            path = d / f"{int(time.time())}.jpg"
            p.screenshot(path=str(path), type="jpeg", quality=60)
            return {"ok": True, "path": f"screenshots/{path.name}", "url": p.url}
        return {"ok": False, "error": f"unknown action {a}"}


def fetch(url: str) -> dict:
    """Cheap text fetch without launching Chromium."""
    import html as htmllib
    import re

    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if blocked(url):
        return {"ok": False, "error": "domain is blocked by the owner's policy"}
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", errors="replace")
        ctype = r.headers.get_content_type()
    if "html" in ctype:
        title = re.search(r"(?is)<title>(.*?)</title>", raw)
        raw = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", raw)
        raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", raw)
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", raw))
        text = "\n".join(" ".join(l.split()) for l in text.splitlines() if l.strip())
        return {"ok": True, "url": url, "title": htmllib.unescape(title.group(1).strip()) if title else "",
                "text": text[:MAX_TEXT * 2]}
    return {"ok": True, "url": url, "title": "", "text": raw[:MAX_TEXT * 2]}


def daemon() -> None:
    state = {"browser": None, "last": time.time()}
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["last"] = time.time()
                try:
                    if req.get("action") == "close":
                        if state["browser"]:
                            state["browser"].ctx.close()
                            state["browser"].pw.stop()
                            state["browser"] = None
                        res = {"ok": True, "text": "browser closed"}
                    else:
                        if state["browser"] is None:
                            state["browser"] = Browser()
                        res = state["browser"].handle(req)
                except Exception as e:
                    res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000]}
            body = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    # Single-threaded on purpose: Playwright sync objects must stay on the thread that made them.
    srv = HTTPServer(("127.0.0.1", PORT), H)

    def reaper():
        while True:
            time.sleep(30)
            if time.time() - state["last"] > IDLE_SECONDS:
                os._exit(0)  # frees Chromium's RAM; restarted on next call

    threading.Thread(target=reaper, daemon=True).start()
    srv.serve_forever()


def call() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    if req.get("action") == "fetch":
        try:
            print(json.dumps(fetch(req["url"])))
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return
    if not _port_open():
        subprocess.Popen([sys.executable, __file__, "daemon"], stdout=subprocess.DEVNULL,
                         stderr=open(HOME / ".browser-daemon.log", "ab"), start_new_session=True)
        for _ in range(100):
            if _port_open():
                break
            time.sleep(0.1)
    data = json.dumps(req).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/", data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            print(resp.read().decode())
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"browser daemon unreachable: {e}"}))


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


if __name__ == "__main__":
    {"daemon": daemon, "call": call}.get(sys.argv[1] if len(sys.argv) > 1 else "call", call)()
