"""Shared state file + control socket used by the bar widget and hotkeys."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("companion.state")

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "hermes-companion"
STATE_FILE = STATE_DIR / "state.json"
TOAST_FILE = STATE_DIR / "toast.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SOCK = RUNTIME_DIR / "hermes-companion.sock"


class State:
    """Process-wide state; every mutation is atomically written to STATE_FILE."""

    def __init__(self, eyes: bool = True):
        self._lock = threading.Lock()
        self.data = {
            "status": "starting",  # starting|watching|listening|thinking|speaking|paused|error
            "eyes": bool(eyes),
            "muted": False,   # mute proactive speech (still answers voice requests)
            "toasts": True,   # on-screen toasts with agent output
            "last_observation": "",
            "last_remark": "",
            "remarks": [],    # [{ts, text, urgency}]
            "last_error": "",
            "ticks": 0,
            "frames_sent": 0,
            "approval": None,  # {id, command, expires} while an approval request waits
            "updated": time.time(),
            "pid": os.getpid(),
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.flush()

    def update(self, **kw):
        with self._lock:
            self.data.update(kw)
            self.data["updated"] = time.time()
            self._write()

    def add_remark(self, text: str, urgency: str):
        with self._lock:
            self.data["remarks"] = ([{"ts": time.time(), "text": text, "urgency": urgency}] + self.data["remarks"])[:20]
            self.data["last_remark"] = text
            self.data["updated"] = time.time()
            self._write()

    def toast(self, text: str, kind: str, note: str = "", request: str = ""):
        """Publish one toast for the shell overlay (atomic write; shell watches the file).
        `request` is the approval request an approval toast's buttons answer."""
        tmp = TOAST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "text": text, "kind": kind, "note": note, "request": request}))
        os.replace(tmp, TOAST_FILE)

    def get(self, k, default=None):
        with self._lock:
            return self.data.get(k, default)

    def flush(self):
        with self._lock:
            self._write()

    def _write(self):
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, STATE_FILE)


class ControlServer(threading.Thread):
    """Unix socket; one line per command: toggle-eyes | listen | toggle-mute | hush | say <text> | ask <text> | status | quit"""

    def __init__(self, handler: Callable[[str], str]):
        super().__init__(daemon=True, name="companion-ctl")
        self.handler = handler
        try:
            SOCK.unlink()
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(SOCK))
        os.chmod(SOCK, 0o600)
        self.srv.listen(4)

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        with conn:
            try:
                conn.settimeout(5)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                cmd = data.decode(errors="replace").strip()
                if not cmd:
                    return
                resp = self.handler(cmd) or "ok"
            except Exception as e:  # noqa: BLE001
                resp = f"error: {e}"
            try:
                conn.sendall((resp + "\n").encode())
            except OSError:
                pass


def send_command(cmd: str, timeout: float = 30.0) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(SOCK))
    s.sendall((cmd.strip() + "\n").encode())
    out = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        out += chunk
        if out.endswith(b"\n"):
            break
    s.close()
    return out.decode(errors="replace").strip()


