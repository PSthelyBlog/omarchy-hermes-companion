"""Tool policy and action layer.

Tool policy, enforced in Hermes's ``pre_tool_call`` hook before a tool runs, Actions on or off:
  screen ticks  no tools: every call made during a screen tick is refused. A tick is driven by what
                is on screen, which whoever wrote a webpage, a message or a window title controls
                too, so it must not be able to read files or reach the network.
  requests      once the conversation holds local data (a file read or search, a helper's command
                output), anything a tool would send off the machine (a web search, a page or image
                fetch, any tool not known to stay local) asks the user first and shows the query or
                URL. Screen-derived history can steer what the model asks for, but cannot move local
                data out unseen.
  helper tasks  with Actions on, each ``delegate_task`` asks the user first and shows its goal.
The hook itself only escalates; Hermes's approval gate then asks the user through the Approver.
Where that gate would let a call through without asking (``approvals.mode: off``, yolo), the
call is refused instead.

Action policy for the helper's commands (fixed, no modes):
  tier 0-3  auto   — anything Hermes' guards don't flag, writes/deletes inside allowed roots
  tier 4    ask    — Hermes dangerous-pattern / Tirith findings, or paths outside allowed roots
  tier 5    refuse — Hermes hardline floors (never bypassable), sudo/pkexec, system-scope systemctl

The companion itself never runs commands: it calls ``delegate_task`` and the child gets
``terminal`` + ``file`` tools. Every child command passes through Hermes' own approval gate
(``check_all_command_guards``), which calls back into :class:`Approver` — toast with
Run/Skip + voice yes/no, 30 s timeout = deny.
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
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, NamedTuple, Optional
from urllib.parse import urlsplit

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
    """Human-in-the-loop: Hermes's approval gates call it, from the tool's thread. Toast buttons write
    DECISION_FILE; voice listens for yes/no. First answer wins; silence = deny.

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
                 allow_session: bool = True, smart_denied: bool = False, **_) -> str:
        """Returns "once" or "deny", never a standing approval. Plugin escalations arrive with a
        placeholder command (`<tool> (plugin approval rule)`) and the hook's message: the
        companion's own carry a card (see _escalate); Hermes's dangerous-command gate passes the
        real command."""
        if _PLUGIN_TARGET_RE.fullmatch(command or ""):
            card = _take_card(description or "") or _Card(
                description or command, "plugin approval", f"May I allow this: {_spoken(description or command)}?",
                {"kind": "approval", "plugin": description or command})
            with self._lock:
                decision = self._ask(card.text, card.note, card.question)
                audit({**card.entry, "decision": decision})
            return "once" if decision else "deny"
        with self._lock:
            tier, reason = classify(command)
            if tier >= 5:
                audit({"kind": "refused", "command": command, "reason": reason})
                self.toast(f"Refused: {command}", "held", reason)
                return "deny"
            decision = self._ask(command, description or reason, f"May I run: {_spoken(command)}?")
            audit({"kind": "approval", "command": command, "reason": description or reason, "decision": decision})
            return "once" if decision else "deny"

    def _ask(self, text: str, note: str, question: str) -> bool:
        """One approval request; the caller holds self._lock. True only for an explicit yes."""
        request = secrets.token_hex(8)
        DECISION_FILE.unlink(missing_ok=True)
        self.set_status("approval")
        self.publish({"id": request, "command": text, "expires": time.time() + APPROVAL_TIMEOUT})
        try:
            self.toast(text, "approval", note, request=request)
            self.speak(f"{question} Say yes or no.")
            decision = self._wait(request, APPROVAL_TIMEOUT)
        finally:
            self.publish(None)   # settled: its card and any late answer to it no longer count
        self.set_status("thinking")
        return decision

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


# ------------------------------------------------------------------ tool policy (pre-tool hook)
FILE_TOOLS = {"write_file", "patch", "read_file"}
SCREEN, REQUEST = "screen", "request"
# Tools whose results bring local data (file contents and names, command output) into the conversation.
LOCAL_DATA_TOOLS = {"read_file", "search_files", "terminal", "process_manage", "patch"}
# Tools that act on this machine only. Any other tool may send its arguments somewhere.
LOCAL_TOOLS = LOCAL_DATA_TOOLS | {"write_file", "todo_list", "delegate_task"}
CONTEXT_SHOWN = 600   # characters of a helper task's context shown on its approval card


class ToolGuard:
    """What the tool policy knows about the conversation.

    `kind` is the turn running now: SCREEN, REQUEST, or None between turns. Turns never overlap
    (brain.TURN_LOCK) and Hermes runs the hook before each tool call of a turn, a helper's
    included, so one process-wide value is right on whichever thread a call runs.
    `local_data` stays set from the first local read until the conversation is forgotten: what a
    read brought in stays in the history, where a later turn could still send it out."""

    def __init__(self):
        self.kind: Optional[str] = None
        self.local_data = False
        self.approver: Optional[Approver] = None
        self._lock = threading.Lock()
        self._forget_after_turn = False

    @contextmanager
    def turn(self, kind: str):
        with self._lock:
            self.kind = kind
        _bind_approver(self.approver)
        try:
            yield
        finally:
            with self._lock:
                self.kind = None
                if self._forget_after_turn:
                    self.local_data = self._forget_after_turn = False

    def note_local_data(self):
        with self._lock:
            self.local_data = True

    def forget(self):
        """The conversation was dropped. A turn still running keeps what it read until it ends
        (its reply is discarded then), so its later calls are still checked."""
        with self._lock:
            if self.kind is None:
                self.local_data = False
            else:
                self._forget_after_turn = True


GUARD = ToolGuard()


def _bind_approver(approver: Optional[Approver]):
    """Hermes looks its approval callback up per thread (and hands it to a turn's tool workers), so
    a turn's thread needs it before Hermes's gate can ask the user about one of its calls."""
    if approver is None:
        return
    try:
        from tools.terminal_tool import set_approval_callback
        set_approval_callback(approver)
    except Exception:
        log.debug("approval callback not bound", exc_info=True)


# ------------------------------------------------------------------ asking (through Hermes's gate)
# A hook must answer at once: Hermes runs it on a worker capped at plugins.hook_callback_timeout
# (30 s by default), then refuses the call and skips the hook for a minute. So the hook only
# escalates ({"action": "approve"}); Hermes's approval gate then calls the Approver, from the tool's
# thread and without that cap. The card to show travels in the escalation's message as a token.
class _Card(NamedTuple):
    text: str        # card body: the command, query, URLs or task
    note: str        # card header
    question: str    # spoken
    entry: dict      # audit entry; the decision is added


_CARD_RE = re.compile(r"companion-ask ([0-9a-f]{12}): ")
_PLUGIN_TARGET_RE = re.compile(r"<[^<>]+> \(plugin approval rule\)")
_cards: "OrderedDict[str, _Card]" = OrderedDict()
_cards_lock = threading.Lock()
MAX_CARDS = 32   # cards Hermes's gate never collected (it did not ask) are dropped oldest first


def _take_card(description: str) -> Optional[_Card]:
    m = _CARD_RE.match(description)
    if not m:
        return None
    with _cards_lock:
        return _cards.pop(m.group(1), None)


def _hermes_gate_skips() -> bool:
    """True when Hermes's approval gate would approve without asking anyone: yolo, or
    `approvals.mode: off`. Also when this Hermes can't say, since then nobody may be asked."""
    try:
        from tools.approval import _yolo_active
        return bool(_yolo_active()) or _hermes_approval_mode() in ("off", "")
    except Exception:
        return True


def _escalate(text: str, note: str, question: str, entry: dict) -> dict:
    """Have Hermes's approval gate put this call to the user before it runs."""
    if _hermes_gate_skips():
        audit({**entry, "decision": False, "reason": "Hermes would not ask (approvals off or yolo)"})
        return _block("Refused by Hermes Companion: this needs the user's yes, and with Hermes's approvals turned off "
                      "(approvals.mode: off, or yolo) nobody would be asked. Tell them.")
    token = secrets.token_hex(6)
    with _cards_lock:
        _cards[token] = _Card(text, note, question, entry)
        while len(_cards) > MAX_CARDS:
            _cards.popitem(last=False)
    return {"action": "approve", "message": f"companion-ask {token}: {_spoken(text, 300)}", "rule_key": "companion"}


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


# ------------------------------------------------------------------ what leaves the machine
def _local_image(url: str) -> bool:
    """vision_analyze loads a path or a data: URL from this machine; anything else with a scheme
    (http:, https:, ftp:, file: …) counts as fetched."""
    return url.lower().startswith("data:") or not re.match(r"[a-z][a-z0-9+.-]*:", url, re.I)


def _brings_local_data(tool_name: str, args: dict) -> bool:
    if tool_name == "vision_analyze":
        return _local_image(str(args.get("image_url", "")).strip())
    return tool_name in LOCAL_DATA_TOOLS


def _leaves_machine(tool_name: str, args: dict) -> bool:
    if tool_name == "vision_analyze":
        return not _local_image(str(args.get("image_url", "")).strip())
    return tool_name not in LOCAL_TOOLS


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def _outbound_card(tool_name: str, args: dict) -> tuple[str, str]:
    """(card text, spoken question) for a call that sends something off the machine."""
    if tool_name == "web_search":
        query = str(args.get("query", ""))
        return f"web search: {query}", f"May I search the web for: {_spoken(query)}?"
    if tool_name == "web_extract":
        urls = [str(u) for u in args["urls"]] if isinstance(args.get("urls"), list) else []
        hosts = ", ".join(sorted({_host(u) for u in urls})) or "the web"
        pages = "a page" if len(urls) == 1 else f"{len(urls)} pages"
        return "open: " + "\n".join(urls), f"May I open {pages} on {_spoken(hosts)}?"
    if tool_name == "vision_analyze":
        url = str(args.get("image_url", ""))
        return f"load image: {url}", f"May I load an image from {_spoken(_host(url))}?"
    return f"{tool_name}: {json.dumps(args, default=str)[:1000]}", f"May I use {tool_name.replace('_', ' ')}?"


def _delegation_card(args: dict) -> Optional[tuple[str, str]]:
    """(card text, spoken question) for a delegate_task call that starts or steers work, else None."""
    action = str(args.get("action") or "spawn")
    if action == "steer":
        message = str(args.get("message", ""))
        return f"tell the helper: {message}", f"May I tell the helper: {_spoken(message)}?"
    if action != "spawn":
        return None   # list / stop: no new work
    # Hermes takes both the `tasks` list and the legacy single-goal shape; show whatever is there.
    tasks = [t for t in (args.get("tasks") if isinstance(args.get("tasks"), list) else []) if isinstance(t, dict)]
    if args.get("goal"):
        tasks.insert(0, args)
    parts, goals = [], []
    for task in tasks:
        goal = str(task.get("goal", "")).strip()
        goals.append(goal)
        part = goal or "(no goal)"
        context = str(task.get("context") or "").strip()
        if context:
            more = len(context) - CONTEXT_SHOWN
            part += "\ncontext: " + context[:CONTEXT_SHOWN] + (f" … (+{more} characters)" if more > 0 else "")
        if task.get("images"):
            part += "\nimages: " + " ".join(str(i) for i in task["images"])
        parts.append(part)
    return "\n\n".join(parts) or "(no task)", f"May I hand this to the helper: {_spoken('; '.join(goals) or 'an empty task')}?"


# ------------------------------------------------------------------ the hook
def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_) -> Optional[dict]:
    """Hermes ``pre_tool_call`` hook (in-process: the companion's tool calls and its helpers'), run
    before the tool. A failure here refuses the call (Hermes would too)."""
    try:
        return _check(tool_name, args if isinstance(args, dict) else {})
    except Exception as e:  # noqa: BLE001
        log.exception("tool policy failed for %s", tool_name)
        return _block(f"Refused by Hermes Companion: its tool policy could not check this call ({type(e).__name__}).")


def _check(tool_name: str, args: dict) -> Optional[dict]:
    if GUARD.kind != REQUEST:
        # Screen ticks get no tools; neither does anything outside a turn.
        audit({"kind": "refused", "tool": tool_name, "reason": "screen tick" if GUARD.kind == SCREEN else "outside a request"})
        return _block("Refused by Hermes Companion: screen ticks have no tools. Answer from what you see.")
    if tool_name == "delegate_task":
        card = _delegation_card(args)
        return _escalate(card[0], "helper task", card[1], {"kind": "delegate", "task": card[0]}) if card else None
    if GUARD.local_data and _leaves_machine(tool_name, args):
        text, question = _outbound_card(tool_name, args)
        return _escalate(text, "leaves this machine; a local file was read", question,
                         {"kind": "outbound", "tool": tool_name, "detail": text})
    directive = _action_policy(tool_name, args)
    if _brings_local_data(tool_name, args):
        GUARD.note_local_data()   # also when it waits for a yes, which may let it run
    return directive


def _hermes_approval_mode() -> str:
    """Hermes's `approvals.mode` (manual / smart / off), "" if this Hermes can't say."""
    try:
        from tools.approval_context import _get_approval_mode
        return str(_get_approval_mode())
    except Exception:
        return ""


def _hermes_will_ask(command: str) -> bool:
    """True when Hermes's own dangerous-command gate will put this command to the Approver: Hermes
    flags it, `approvals.mode` is manual and yolo is off. Otherwise that gate may let it through
    unasked (smart mode's guardian model, `off`, yolo), so the companion escalates it itself."""
    try:
        from tools.approval import _yolo_active
        from tools.approval_detection import detect_dangerous_command
        return bool(detect_dangerous_command(command)[0]) and _hermes_approval_mode() == "manual" and not _yolo_active()
    except Exception:
        return False


def _action_policy(tool_name: str, args: dict) -> Optional[dict]:
    """The helper's commands and file writes:
    - terminal: tier 5 → block; tier 4 → ask the user (toast + voice), tier 0-3 runs.
    - file writes outside allowed roots and reads of credential files → ask the user."""
    if tool_name == "terminal":
        cmd = str(args.get("command", ""))
        tier, reason = classify(cmd)
        if tier >= 5:
            audit({"kind": "refused", "command": cmd, "reason": reason})
            return _block(f"Refused by Hermes Companion policy ({reason}). Tell the user instead.")
        if tier == 4:
            if _hermes_will_ask(cmd):
                return None   # asking here too would ask twice
            return _escalate(cmd, reason, f"May I run: {_spoken(cmd)}?", {"kind": "approval", "command": cmd, "reason": reason})
        audit({"kind": "run", "command": cmd})
        return None
    if tool_name in FILE_TOOLS:
        path = str(args.get("path", ""))
        if not path:
            return None
        bad = outside_allowed_roots(path if path.startswith(("/", "~")) else str(Path.cwd() / path))
        if bad and tool_name != "read_file":
            return _escalate(f"{tool_name}: {path}", "outside your home folder or in a dotfile",
                             f"May I change {_spoken(path)}?", {"kind": "approval", "tool": tool_name, "path": path})
        if tool_name == "read_file" and re.search(r"(\.ssh/|\.gnupg/|\.aws/|/\.env$|auth\.json|id_(rsa|ed25519)|\.pem$)", path):
            return _escalate(f"read_file: {path}", "credential file", f"May I read {_spoken(path)}?",
                             {"kind": "approval", "tool": tool_name, "path": path})
        if tool_name != "read_file":
            audit({"kind": "write", "tool": tool_name, "path": path})
    return None


def install_hook(approver: Approver):
    GUARD.approver = approver
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
