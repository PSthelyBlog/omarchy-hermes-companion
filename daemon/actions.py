"""Action layer: lets the companion delegate shell/file work to a Hermes subagent.

Policy (fixed, no modes):
  tier 0-3  auto   — anything Hermes' guards don't flag, writes/deletes inside allowed roots
  tier 4    ask    — Hermes dangerous-pattern / Tirith findings, or paths outside allowed roots
  tier 5    refuse — Hermes hardline floors (never bypassable), sudo/pkexec, system-scope systemctl

The companion itself never runs commands: it calls ``delegate_task`` and the child gets
``terminal`` + ``file`` tools. Every child command passes through Hermes' own approval gate
(``check_all_command_guards``), which calls back into :class:`Approver` — toast with
Run/Skip + voice yes/no, 30 s timeout = deny. Screen ticks never see the delegate tool.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("companion.actions")

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "hermes-companion"
AUDIT_FILE = STATE_DIR / "actions.jsonl"
DECISION_FILE = STATE_DIR / "decision.json"   # written by the shell toast buttons

HOME = Path.home().resolve()
APPROVAL_TIMEOUT = 30.0

# Tier 5 additions on top of Hermes' hardline floors.
_REFUSE_RE = re.compile(
    r"(^|[\s;&|(])(sudo|doas|pkexec|su)\b"
    r"|(^|[\s;&|(])systemctl\s+(?!--user\b)(start|stop|restart|enable|disable|mask|poweroff|reboot|halt|kexec)\b"
    r"|(^|[\s;&|(])(shutdown|reboot|poweroff|halt|init\s+[06]|telinit)\b"
    r"|(^|[\s;&|(])(mkfs|fdisk|parted|wipefs|dd)\b"
    r"|/etc/(sudoers|passwd|shadow)\b",
    re.I,
)


# ------------------------------------------------------------------ classification
def _paths_in(command: str) -> list[Path]:
    """Absolute or ~ paths mentioned in a command (best effort; used for the roots check)."""
    out = []
    try:
        toks = shlex.split(command, posix=True)
    except ValueError:
        toks = command.split()
    for t in toks:
        t = t.split("=", 1)[1] if t.startswith("--") and "=" in t else t
        if t.startswith(("/", "~")):
            try:
                out.append(Path(os.path.expanduser(t)).resolve())
            except Exception:
                pass
    return out


def outside_allowed_roots(command: str) -> Optional[str]:
    """First path that is outside $HOME or is a dotfile/dot-dir under $HOME, else None."""
    for p in _paths_in(command):
        if p == Path("/tmp") or str(p).startswith(("/tmp/", "/dev/null")):
            continue
        try:
            rel = p.relative_to(HOME)
        except ValueError:
            return str(p)
        if any(part.startswith(".") for part in rel.parts):
            return str(p)
    return None


def classify(command: str) -> tuple[int, str]:
    """Return (tier, reason). Uses Hermes' detectors; 5 = refuse, 4 = ask, else 0."""
    from tools.approval_detection import detect_dangerous_command, detect_hardline_command

    hard, desc = detect_hardline_command(command)
    if hard:
        return 5, f"hardline: {desc}"
    if _REFUSE_RE.search(command):
        return 5, "privileged / system-level command"
    bad_path = outside_allowed_roots(command)
    mutating = re.search(r"(^|[\s;&|(])(rm|mv|cp|tee|truncate|shred|chmod|chown|ln|sed\s+-i|>+)\b", command)
    if bad_path and mutating:
        return 4, f"touches a path outside allowed roots: {bad_path}"
    danger, key, desc = detect_dangerous_command(command)
    if danger:
        return 4, desc or key or "dangerous pattern"
    if re.search(r"(^|[\s;&|(])(pkill|killall|kill\s+-9)\b", command):
        return 4, "kills processes by name"
    if re.search(r"(\.ssh/|\.gnupg/|\.aws/|\.env\b|auth\.json|credentials|id_(rsa|ed25519)|\.pem\b|token)", command, re.I):
        return 4, "reads or touches credentials"
    return 0, "ok"


# ------------------------------------------------------------------ audit
def audit(entry: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), **entry}
    with AUDIT_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ------------------------------------------------------------------ approval
class Approver:
    """Human-in-the-loop for tier-4 commands. Toast buttons write DECISION_FILE; voice
    listens for yes/no. First answer wins; silence = deny.

    Each request gets an id, published as `approval` while it waits and carried by its toast. A
    button answer names that id (see decide()), so a card left over from an earlier, settled
    request can never answer a newer one."""

    def __init__(self, toast: Callable[..., None], speak: Callable[[str], None],
                 listen_yes_no: Callable[[float, threading.Event], Optional[bool]],
                 set_status: Callable[[str], None],
                 publish: Callable[[Optional[dict]], None] = lambda pending: None):
        self.toast, self.speak, self.listen_yes_no, self.set_status = toast, speak, listen_yes_no, set_status
        self.publish = publish
        self._lock = threading.Lock()

    def __call__(self, command: str, description: str, *, allow_permanent: bool = True,
                 allow_session: bool = True, smart_denied: bool = False) -> str:
        # Hermes calls this from the (child's) tool thread with the flagged command. For plugin
        # escalations it passes a placeholder; the real command then sits in `description`.
        if command.startswith("<") and ": " in (description or ""):
            command = description.split(": ", 1)[1]
        with self._lock:
            tier, reason = classify(command)
            if tier >= 5:
                audit({"kind": "refused", "command": command, "reason": reason})
                self.toast(f"Refused: {command}", "held", reason)
                return "deny"
            request = secrets.token_hex(8)
            DECISION_FILE.unlink(missing_ok=True)
            self.set_status("approval")
            self.publish({"id": request, "command": command, "expires": time.time() + APPROVAL_TIMEOUT})
            try:
                self.toast(command, "approval", description or reason, request=request)
                self.speak(f"May I run: {_spoken(command)}? Say yes or no.")
                decision = self._wait(request, APPROVAL_TIMEOUT)
            finally:
                self.publish(None)   # settled: its card and any late answer to it no longer count
            self.set_status("thinking")
            audit({"kind": "approval", "command": command, "reason": description or reason, "decision": decision})
            return "once" if decision else "deny"

    def _wait(self, request: str, timeout: float) -> bool:
        result: dict = {}
        done = threading.Event()   # set once settled; also stops this request's voice listener

        def voice():
            v = self.listen_yes_no(timeout, done)
            if v is not None and not done.is_set():
                result.setdefault("v", v)
                done.set()

        threading.Thread(target=voice, daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline and not done.is_set():
            try:
                d = json.loads(DECISION_FILE.read_text())
            except (OSError, ValueError):
                d = None
            # Only an answer naming this request counts. decide() already refuses others; one
            # that slips past (a request settled between that check and the write) stays unread.
            if isinstance(d, dict) and d.get("request") == request:
                result.setdefault("v", d.get("approve") is True)
                done.set()
                break
            time.sleep(0.2)
        done.set()
        DECISION_FILE.unlink(missing_ok=True)
        return bool(result.get("v", False))


def decide(arg: str, pending: Optional[dict]) -> str:
    """`--ctl decide <request> <yes|no>`: answer the approval request with that id.

    `pending` is the request waiting now (the Approver's published `approval`). An answer for any
    other request is refused: its card outlived it, and nothing waits for that answer any more."""
    parts = arg.split()
    if len(parts) != 2:
        return "usage: decide <request> <yes|no>"
    request, answer = parts
    if not pending or request != pending.get("id"):
        return "stale: no such request is waiting"
    DECISION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DECISION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"request": request, "approve": answer.lower() in ("yes", "run", "approve", "1", "true")}))
    os.replace(tmp, DECISION_FILE)
    return "decided"


def _spoken(command: str, limit: int = 120) -> str:
    c = command.strip().replace("\n", " ")
    return c if len(c) <= limit else c[:limit] + "… and more"


YES_RE = re.compile(r"\b(yes|yeah|yep|sure|go ahead|do it|ok(ay)?|approve|run it)\b", re.I)
NO_RE = re.compile(r"\b(no|nope|don'?t|stop|cancel|skip|deny|abort)\b", re.I)


def parse_yes_no(text: str) -> Optional[bool]:
    if not text:
        return None
    if NO_RE.search(text):
        return False
    if YES_RE.search(text):
        return True
    return None


# ------------------------------------------------------------------ pre-tool hook
FILE_TOOLS = {"write_file", "patch", "read_file"}


def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_) -> Optional[dict]:
    """Hermes ``pre_tool_call`` hook (in-process, applies to the companion and its children).
    - terminal: tier 5 → block; tier 4 → escalate to the approval gate (which calls Approver).
    - file writes: outside allowed roots → escalate; reads of credentials → escalate.
    The gate is then answered by the toast/voice Approver; tier 0-3 passes untouched."""
    args = args or {}
    if tool_name == "terminal":
        cmd = str(args.get("command", ""))
        tier, reason = classify(cmd)
        if tier >= 5:
            audit({"kind": "refused", "command": cmd, "reason": reason})
            return {"action": "block", "message": f"Refused by Hermes Companion policy ({reason}). Tell the user instead."}
        if tier == 4:
            # Hermes' own dangerous-pattern gate already reaches the Approver with the real
            # command text; only escalate the companion-specific tier-4 cases it doesn't know.
            from tools.approval_detection import detect_dangerous_command
            if not detect_dangerous_command(cmd)[0]:
                return {"action": "approve", "message": f"{reason}: {cmd}", "rule_key": "companion"}
            return None
        audit({"kind": "run", "command": cmd})
        return None
    if tool_name in FILE_TOOLS:
        path = str(args.get("path", ""))
        if not path:
            return None
        bad = outside_allowed_roots(path if path.startswith(("/", "~")) else str(Path.cwd() / path))
        if bad and tool_name != "read_file":
            return {"action": "approve", "message": f"{tool_name} outside allowed roots: {bad}", "rule_key": "companion"}
        if tool_name == "read_file" and re.search(r"(\.ssh/|\.gnupg/|\.aws/|/\.env$|auth\.json|id_(rsa|ed25519)|\.pem$)", path):
            return {"action": "approve", "message": f"read credential file: {path}", "rule_key": "companion"}
        if tool_name != "read_file":
            audit({"kind": "write", "tool": tool_name, "path": path})
    return None


def install_hook():
    from hermes_cli.plugins import get_plugin_manager
    pm = get_plugin_manager()
    pm._hooks.setdefault("pre_tool_call", []).append(pre_tool_call)


# ------------------------------------------------------------------ child prompt
CHILD_RULES = """You are a helper executing a task on {user}'s Linux desktop (Omarchy / Arch, Hyprland).
Rules you must follow:
- Work inside {home}; never modify dotfiles/dot-directories or anything outside it without being told explicitly.
- Never use sudo/pkexec, never touch system services (systemctl without --user), never wipe disks or reset git history.
- Prefer reversible operations (trash over rm, git stash over checkout --, --dry-run first when available).
- Some commands will pause for the user's approval; if denied, do not retry the same command another way — report it.
- Be brief: when done, reply with 1-3 sentences a person can listen to (what you did, the result, anything they must check).
"""
