"""Screen perception: focused-window metadata + change-detected screenshots.

Screenshots stay in RAM (grim writes to stdout). Nothing is persisted.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("companion.perception")

# Window classes / title fragments we never look at.
SENSITIVE_CLASS_RE = re.compile(
    r"(1password|bitwarden|keepass|keepassxc|proton.?pass|gnome-keyring|seahorse|"
    r"polkit|pinentry|hyprlock|omarchy-lock|swaylock|gcr-prompter|kwalletd)",
    re.I,
)
SENSITIVE_TITLE_RE = re.compile(
    r"(private browsing|incognito|inprivate|password|passcode|2fa|one-time|otp|"
    r"bank|banque|revolut|paypal|stripe dashboard|credit card|carte bancaire|"
    r"private window|fenêtre privée|navigation privée)",
    re.I,
)


@dataclass
class WindowInfo:
    cls: str = ""
    title: str = ""
    fullscreen: bool = False
    workspace: str = ""
    monitor: str = ""
    pid: int = 0

    @property
    def sensitive(self) -> bool:
        return bool(SENSITIVE_CLASS_RE.search(self.cls) or SENSITIVE_TITLE_RE.search(self.title))


@dataclass
class Frame:
    window: WindowInfo
    jpeg: Optional[bytes]  # None if privacy-skipped / unchanged
    changed: bool
    idle_seconds: float
    ts: float = field(default_factory=time.time)
    phash: Optional[str] = None

    def data_url(self) -> Optional[str]:
        if not self.jpeg:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(self.jpeg).decode()


def _run(cmd: list[str], timeout: float = 5.0) -> bytes:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False).stdout


def active_window() -> WindowInfo:
    try:
        d = json.loads(_run(["hyprctl", "activewindow", "-j"]) or b"{}")
    except Exception:
        return WindowInfo()
    if not d:
        return WindowInfo()
    ws = d.get("workspace") or {}
    return WindowInfo(
        cls=d.get("class") or "",
        title=d.get("title") or "",
        fullscreen=bool(d.get("fullscreen")),
        workspace=str(ws.get("name") or ws.get("id") or ""),
        monitor=str(d.get("monitor", "")),
        pid=int(d.get("pid") or 0),
    )


def focused_monitor() -> Optional[str]:
    try:
        mons = json.loads(_run(["hyprctl", "monitors", "-j"]) or b"[]")
        for m in mons:
            if m.get("focused"):
                return m["name"]
        return mons[0]["name"] if mons else None
    except Exception:
        return None


def session_locked() -> bool:
    try:
        out = _run(["pgrep", "-x", "hyprlock"]).strip()
        if out:
            return True
        # Omarchy 4 lock lives in omarchy-shell; ask it.
        out = _run(["omarchy-shell", "shell", "isLocked"], timeout=2).decode().strip().lower()
        return out in ("true", "1", "yes")
    except Exception:
        return False


def idle_seconds() -> float:
    """Seconds since last input. Uses hyprctl's idle inhibit-free heuristic via `loginctl`.
    Falls back to 0 if unavailable."""
    try:
        out = _run(["loginctl", "show-session", "auto", "-p", "IdleSinceHint"], timeout=2).decode()
        # IdleSinceHint is usec since epoch, 0 when not idle
        val = int(out.strip().split("=")[-1] or 0)
        if val == 0:
            return 0.0
        return max(0.0, time.time() - val / 1e6)
    except Exception:
        return 0.0


def mic_in_use() -> bool:
    """True if some *other* app currently records the mic (meeting / call).

    Our own wake-word listener (PortAudio → "PipeWire ALSA [python3.11]") is ignored,
    as is the Hermes desktop app's own voice capture."""
    try:
        out = _run(["pactl", "list", "source-outputs"]).decode()
    except Exception:
        return False
    me = os.getpid()
    for block in out.split("Source Output #")[1:]:
        pid = re.search(r'application\.process\.id = "(\d+)"', block)
        if pid and int(pid.group(1)) == me:
            continue
        name = re.search(r'application\.name = "([^"]*)"', block)
        if name and re.search(r"PipeWire ALSA \[python", name.group(1)):
            continue  # PortAudio streams (ours / hermes) don't carry a pid
        return True
    return False


def screen_shared() -> bool:
    """True while a screencast / recorder is active (portal screencast session, wf-recorder, gpu-screen-recorder, obs)."""
    try:
        if _run(["pgrep", "-x", "wf-recorder|obs"]).strip():
            return True
        # The kernel cuts process names to 15 chars ("gpu-screen-reco"), so -x can never match it;
        # match the command line the way omarchy-capture-screenrecording checks its own recording.
        if _run(["pgrep", "-f", "^gpu-screen-recorder"]).strip():
            return True
        out = _run(["pw-dump"], timeout=3).decode()
        # portal screencast nodes are named like "xdg-desktop-portal-hyprland" video sources with a running consumer
        return bool(re.search(r'"media\.class":\s*"Stream/Input/Video"', out)) and "xdg-desktop-portal" in out
    except Exception:
        return False


def grab_jpeg(monitor: Optional[str], max_width: int = 1280, quality: int = 70) -> Optional[bytes]:
    cmd = ["grim", "-t", "jpeg", "-q", str(quality)]
    if monitor:
        cmd += ["-o", monitor]
    if max_width:
        cmd += ["-s", "1"]  # grim scale; we resize below with PIL for exactness
    cmd.append("-")
    raw = _run(cmd, timeout=6)
    if not raw:
        return None
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width > max_width:
            h = int(im.height * max_width / im.width)
            im = im.resize((max_width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


def perceptual_hash(jpeg: bytes, size: int = 16) -> str:
    """Simple dHash (difference hash) — cheap and robust to minor noise."""
    from PIL import Image

    im = Image.open(io.BytesIO(jpeg)).convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(im.getdata())
    bits = []
    for y in range(size):
        row = px[y * (size + 1) : (y + 1) * (size + 1)]
        bits.extend("1" if row[x] > row[x + 1] else "0" for x in range(size))
    return "%x" % int("".join(bits), 2)


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


class Perceiver:
    def __init__(self, change_threshold: int = 12, max_width: int = 1280):
        self.change_threshold = change_threshold
        self.max_width = max_width
        self._last_hash: Optional[str] = None
        self._last_title: str = ""

    def observe(self, eyes_enabled: bool = True) -> Frame:
        win = active_window()
        idle = idle_seconds()
        if not eyes_enabled or win.sensitive or session_locked():
            self._last_hash = None
            return Frame(window=win, jpeg=None, changed=False, idle_seconds=idle)

        jpeg = grab_jpeg(focused_monitor(), self.max_width)
        if not jpeg:
            return Frame(window=win, jpeg=None, changed=False, idle_seconds=idle)
        try:
            h = perceptual_hash(jpeg)
        except Exception:
            h = None
        changed = True
        if h and self._last_hash:
            changed = hamming(h, self._last_hash) >= self.change_threshold or win.title != self._last_title
        self._last_hash = h
        self._last_title = win.title
        return Frame(window=win, jpeg=jpeg if changed else None, changed=changed, idle_seconds=idle, phash=h)
