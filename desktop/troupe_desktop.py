"""Troupe Desktop: a Windows app for your Troupe server.

- Opens Troupe in its own native window (Edge WebView2), with a Start menu entry and a tray icon.
- Connects to the server by itself through an SSH tunnel (Windows' built-in OpenSSH), reconnects
  when the network drops, and signs in with the server's admin token, so there's nothing to paste.
- The server's web UI never needs to be exposed to the internet.
- Closing the window keeps Troupe in the tray and shows a notification when an agent needs approval
  or asks you something. Quit from the tray icon.
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
import webbrowser
from pathlib import Path

import webview

APP_NAME = "Troupe"
# Not under AppData: packaged (MSIX) launchers virtualize AppData writes, so a config written there
# from one process can be invisible to another.
CONFIG_DIR = Path.home() / ".troupe-desktop"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "desktop.log"
SINGLE_INSTANCE_PORT = 47833
SSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULTS = {"host": "", "user": "ubuntu", "key_path": "", "remote_port": 8080, "local_port": 0,
            "token_command": "sudo cat /var/lib/troupe/admin_token", "close_to_tray": True}
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback must skip proxies


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def log(msg: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ssh_base(cfg: dict) -> list[str]:
    return [str(SSH), "-i", cfg["key_path"], "-p", str(cfg.get("ssh_port", 22)),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]


class Tunnel:
    """Keeps `ssh -N -L` running, restarting it with backoff whenever it exits."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.port = int(cfg.get("local_port") or 0) or free_port()
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        self.last_error = ""
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _loop(self) -> None:
        delay = 2
        while not self.stopping:
            argv = ssh_base(self.cfg) + ["-o", "ExitOnForwardFailure=yes", "-N",
                                         "-L", f"127.0.0.1:{self.port}:127.0.0.1:{self.cfg['remote_port']}",
                                         f"{self.cfg['user']}@{self.cfg['host']}"]
            started = time.time()
            self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         creationflags=NO_WINDOW)
            _, err = self.proc.communicate()
            if self.stopping:
                break
            lines = (err or b"").decode(errors="replace").strip().splitlines()
            self.last_error = lines[-1] if lines else "tunnel closed"
            log(f"tunnel exited: {self.last_error}")
            delay = 2 if time.time() - started > 60 else min(delay * 2, 30)
            time.sleep(delay)

    def healthy(self) -> bool:
        try:
            with _direct.open(f"http://127.0.0.1:{self.port}/healthz", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def stop(self) -> None:
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def fetch_token(cfg: dict) -> str:
    if cfg.get("token"):
        return cfg["token"]
    r = subprocess.run(ssh_base(cfg) + [f"{cfg['user']}@{cfg['host']}", cfg["token_command"]],
                       capture_output=True, text=True, timeout=40, creationflags=NO_WINDOW)
    token = r.stdout.strip()
    if r.returncode != 0 or not token:
        raise RuntimeError((r.stderr.strip().splitlines() or ["could not read the admin token"])[-1])
    return token


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    :root {{ --bg:#f6f6f4; --panel:#fff; --ink:#1b1d21; --muted:#6b6f76; --line:#e3e3df; --accent:#2f6fde; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg:#14161a; --panel:#1c1f24; --ink:#e8e8e6; --muted:#9a9ea6; --line:#2c3037; --accent:#5b8ff0; }} }}
    body {{ margin:0; height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
           font:15px/1.5 system-ui,"Segoe UI",sans-serif; }}
    .card {{ width:min(460px, calc(100% - 32px)); background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:20px; }} p {{ color:var(--muted); margin:6px 0 14px; }}
    label {{ display:grid; gap:4px; font-size:13px; color:var(--muted); margin-bottom:10px; }}
    input {{ font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--ink); }}
    .row {{ display:flex; gap:8px; }} .row input {{ flex:1; }}
    button {{ font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; }}
    button.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
    .spin {{ width:28px; height:28px; border:3px solid var(--line); border-top-color:var(--accent); border-radius:50%; animation:s 1s linear infinite; margin-bottom:12px; }}
    @keyframes s {{ to {{ transform:rotate(360deg); }} }} code {{ font-size:12px; }} .err {{ color:#c0392b; overflow-wrap:anywhere; }}
    </style><title>{title}</title></head><body><div class="card">{body}</div></body></html>"""


CONNECTING = page("Troupe", '<div class="spin"></div><h1>Connecting to your team…</h1>'
                            '<p id="s">Opening a secure tunnel to your Troupe server.</p>')


def error_page(msg: str) -> str:
    return page("Troupe", f"""<h1>Can't reach your Troupe server</h1><p class="err">{msg}</p>
      <p>Check that the server is running and that its firewall allows SSH from your current IP address
      (a VPN or proxy changes that address).</p>
      <p id="auto">Retrying automatically in <span id="n">20</span>s…</p>
      <div class="row"><button class="primary" onclick="pywebview.api.retry()">Try again now</button>
      <button onclick="clearInterval(t); document.getElementById('auto').remove(); pywebview.api.settings()">Connection settings</button></div>
      <script>
        let left = 20;
        const t = setInterval(() => {{
          left -= 1; const n = document.getElementById('n'); if (n) n.textContent = left;
          if (left <= 0) {{ clearInterval(t); pywebview.api.retry(); }}
        }}, 1000);
      </script>""")


def settings_page(cfg: dict) -> str:
    v = {k: str(cfg.get(k, "")).replace('"', "&quot;") for k in ("host", "user", "key_path", "remote_port")}
    return page("Troupe setup", f"""<h1>Connect to your Troupe server</h1>
      <p>Troupe Desktop reaches the server over SSH, so its web UI never has to be public.</p>
      <label>Server address<input id="host" value="{v['host']}" placeholder="203.0.113.10"></label>
      <label>SSH user<input id="user" value="{v['user']}"></label>
      <label>SSH private key<div class="row"><input id="key_path" value="{v['key_path']}">
        <button onclick="pick()">Browse…</button></div></label>
      <label>Troupe port on the server<input id="remote_port" value="{v['remote_port']}"></label>
      <div class="row"><button class="primary" onclick="save()">Save and connect</button></div>
      <script>
        async function pick() {{ const p = await pywebview.api.pick_key(); if (p) document.getElementById('key_path').value = p; }}
        function save() {{ pywebview.api.save({{host: host.value.trim(), user: user.value.trim(),
          key_path: key_path.value.trim(), remote_port: parseInt(remote_port.value) || 8080}}); }}
      </script>""")


class App:
    def __init__(self):
        self.cfg = load_config()
        self.tunnel: Tunnel | None = None
        self.window: webview.Window | None = None
        self.tray = None
        self.quitting = False
        self.connected_url = ""
        self.seen_approvals: set[str] = set()
        self.seen_questions: int = 0

    # --- js api (exposed to the setup and error pages) ------------------------------------------
    class Api:
        """Methods the setup/error pages call. Holds no attributes on purpose: pywebview walks the
        js_api object's attributes to expose them, and walking into the app/window hangs it."""

        def retry(self):
            threading.Thread(target=_APP.connect, daemon=True).start()

        def settings(self):
            _APP.window.load_html(settings_page(_APP.cfg))

        def pick_key(self):
            r = _APP.window.create_file_dialog(webview.OPEN_DIALOG, directory=str(Path.home() / ".ssh"))
            return r[0] if r else ""

        def save(self, values: dict):
            _APP.cfg.update(values)
            save_config(_APP.cfg)
            self.retry()

    # --- connection --------------------------------------------------------------------------------
    def connect(self) -> None:
        try:
            self._connect()
        except Exception as e:
            import traceback
            log("connect failed: " + traceback.format_exc())
            try:
                self.window.load_html(error_page(f"{type(e).__name__}: {e}"))
            except Exception:
                pass

    def _connect(self) -> None:
        w = self.window
        log(f"connecting to {self.cfg.get('user')}@{self.cfg.get('host')}")
        if not self.cfg.get("host") or not self.cfg.get("key_path"):
            w.load_html(settings_page(self.cfg))
            return
        if not SSH.exists():
            w.load_html(error_page("Windows OpenSSH client not found. Install it from Settings → System → "
                                   "Optional features → OpenSSH Client."))
            return
        w.load_html(CONNECTING)
        log("starting tunnel")
        if self.tunnel:
            self.tunnel.stop()
        self.tunnel = Tunnel(self.cfg)
        self.tunnel.start()
        for _ in range(40):
            if self.tunnel.healthy():
                break
            time.sleep(0.5)
        else:
            self.tunnel.stop()
            w.load_html(error_page(self.tunnel.last_error or "The tunnel did not come up."))
            return
        try:
            token = fetch_token(self.cfg)
        except Exception as e:
            w.load_html(error_page(f"Connected, but couldn't read the admin token: {e}"))
            return
        self.token = token
        self.connected_url = f"http://127.0.0.1:{self.tunnel.port}/"
        log(f"connected on {self.connected_url}")
        # The web UI moves ?token= into its own storage and strips it from the address at once.
        w.load_url(f"{self.connected_url}?token={token}")

    # --- tray and notifications -------------------------------------------------------------------
    def start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return
        img = Image.open(resource("troupe.ico"))
        menu = pystray.Menu(
            pystray.MenuItem("Open Troupe", lambda: self.show(), default=True),
            pystray.MenuItem("Open in browser", lambda: self.connected_url and webbrowser.open(self.connected_url)),
            pystray.MenuItem("Reconnect", lambda: threading.Thread(target=self.connect, daemon=True).start()),
            pystray.MenuItem("Connection settings", lambda: (self.show(), self.window.load_html(settings_page(self.cfg)))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.quit()),
        )
        self.tray = pystray.Icon("troupe", img, APP_NAME, menu)
        self.tray.run_detached()

    def notify(self, title: str, msg: str) -> None:
        if self.tray:
            try:
                self.tray.notify(msg[:240], title)
            except Exception:
                pass

    def watch(self) -> None:
        """Toast for new approvals and agent questions, so the window can stay closed."""
        first = True
        while not self.quitting:
            time.sleep(10)
            if not self.connected_url or not getattr(self, "token", ""):
                continue
            try:
                req = urllib.request.Request(self.connected_url + "api/state",
                                             headers={"Authorization": f"Bearer {self.token}"})
                with _direct.open(req, timeout=10) as r:
                    state = json.loads(r.read())
            except Exception:
                continue
            for a in state.get("approvals", []):
                if a["id"] not in self.seen_approvals:
                    self.seen_approvals.add(a["id"])
                    if not first:
                        self.notify(f"{a['agent_name']} needs your approval", f"{a['tool']}: {a['reason']}")
            newest_q = 0
            for room in state.get("rooms", []):
                last = room.get("last_message") or {}
                newest_q = max(newest_q, last.get("id", 0))
            if not first and newest_q > self.seen_questions and not self.window_visible():
                self.notify("Troupe", "New messages from your agents")
            self.seen_questions = max(self.seen_questions, newest_q)
            first = False

    def window_visible(self) -> bool:
        return getattr(self, "_visible", True)

    # --- window lifecycle --------------------------------------------------------------------------
    def show(self) -> None:
        if self.window:
            self.window.show()
            self.window.restore()
            self._visible = True

    def on_closing(self):
        if self.quitting or not self.cfg.get("close_to_tray", True) or not self.tray:
            return True
        self.window.hide()
        self._visible = False
        if not self.cfg.get("_told_tray"):
            self.notify("Troupe is still running", "Your agents keep working. Open Troupe from the tray icon.")
            self.cfg["_told_tray"] = True
            save_config(self.cfg)
        return False

    def quit(self) -> None:
        self.quitting = True
        if self.tunnel:
            self.tunnel.stop()
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()

    def single_instance_server(self) -> bool:
        """Return False if another copy is running (and ask it to show its window)."""
        try:
            self._lock = socket.socket()
            self._lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
            self._lock.listen(1)
        except OSError:
            try:
                with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2) as s:
                    s.sendall(b"show")
            except OSError:
                pass
            return False

        def serve():
            while True:
                conn, _ = self._lock.accept()
                with conn:
                    if conn.recv(16) == b"show":
                        self.show()
        threading.Thread(target=serve, daemon=True).start()
        return True

    def run(self) -> None:
        if not self.single_instance_server():
            return
        self.window = webview.create_window(APP_NAME, html=CONNECTING, js_api=App.Api(),
                                            width=1200, height=820, min_size=(380, 520),
                                            background_color="#14161a", text_select=True)
        self.window.events.closing += self.on_closing

        def startup():
            # Start the tray only once the window's event loop is running; doing it earlier
            # leaves WebView2 calls from worker threads waiting forever.
            self.start_tray()
            threading.Thread(target=self.watch, daemon=True).start()
            self.connect()

        webview.start(startup, private_mode=False,
                      storage_path=str(CONFIG_DIR / "webview"), icon=str(resource("troupe.ico")))
        self.quitting = True
        if self.tunnel:
            self.tunnel.stop()
        if self.tray:
            self.tray.stop()


_APP: "App"

if __name__ == "__main__":
    try:
        _APP = App()
        _APP.run()
    except Exception as e:  # last resort: log it so support is possible
        log(f"fatal: {type(e).__name__}: {e}")
        raise
