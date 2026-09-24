#!/usr/bin/env python3
"""Hermes Companion daemon — always-on screen-aware voice assistant for Omarchy."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from state import ControlServer, State  # noqa: E402
from actions import Approver, audit, install_hook, parse_yes_no  # noqa: E402

try:
    from brain import CompanionAgent, ModelSpec  # noqa: E402
    from catalog import Catalog  # noqa: E402
    from perception import Perceiver, screen_shared  # noqa: E402
    from policy import PolicyConfig, SpeechPolicy  # noqa: E402
    if "--ctl" not in sys.argv:
        import run_agent  # noqa: E402,F401  (real Hermes probe; brain imports it lazily)
except Exception as _e:  # Hermes missing/broken: record it for the widget and exit (no restart loop)
    if "--ctl" not in sys.argv:
        State().update(status="error", last_error=f"Hermes runtime unavailable: {_e}")
        print(f"hermes-companion: cannot import Hermes runtime: {_e}", file=sys.stderr)
        sys.exit(78)  # EX_CONFIG
    raise

log = logging.getLogger("companion")

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy/plugins/hermes.companion/companion.json"
DEFAULTS = {
    "vision": {"model": "", "effort": "low", "thinking": True},      # model "" = Hermes' main model (config.yaml)
    "reasoning": {"model": "", "effort": "low", "thinking": True},   # model "" = same as vision
    "user_name": os.environ.get("USER", "User").capitalize(),
    "tick_seconds": 25,
    "change_threshold": 12,
    "max_width": 1280,
    "min_gap_seconds": 300,
    "urgent_gap_seconds": 60,
    "max_per_hour": 8,
    "notify": True,
    "actions": False,   # let voice/text requests delegate shell/file work to a Hermes subagent
    # Start with the screen-watching "eyes" on (True, matches the tool's original
    # always-on behaviour) or off (False), e.g. for on-demand/ask-only use where nothing
    # persists across restarts anyway and proactive screen ticks aren't wanted by default.
    "eyes": True,
    # Reply language: "auto" (default) asks the model to answer in whichever language
    # {{USER}} addressed it in, so the companion follows the user without config for the
    # common case; a fixed value (e.g. "German", "Spanish") pins every reply to that
    # language regardless of what the user typed/spoke in.
    "language": "auto",
    # Free-text description of who the user is / how they work, injected into the system
    # prompt. Empty (default) uses a neutral one-liner; anything else replaces it entirely.
    "user_context": "",
    # Where the toast stack spawns and how far it sits from that corner. anchor is one of
    # top-right/top-left/bottom-right/bottom-left; margin_x/margin_y are extra pixels added
    # on top of the bar clearance + base gap the shell already reserves. bottom-right is the
    # default because toasts were hardcoded there before the corner became configurable.
    "toast_position": {"anchor": "bottom-right", "margin_x": 0, "margin_y": 0},
}

_TOAST_ANCHORS = ("top-right", "top-left", "bottom-right", "bottom-left")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("bad companion.json, using defaults")
    # migrate pre-split keys: model/provider/effort/thinking -> vision
    legacy = {k: cfg.pop(k) for k in ("model", "provider", "effort", "thinking") if k in cfg}
    if legacy and "vision" not in cfg:
        cfg["vision"] = {"model": f"{legacy.get('provider', 'anthropic')}:{legacy.get('model', '')}", "effort": legacy.get("effort", "low"), "thinking": legacy.get("thinking", True)}
    if legacy:
        try:  # rewrite the file without the legacy keys
            data = json.loads(CONFIG_FILE.read_text())
            for k in ("model", "provider", "effort", "thinking"):
                data.pop(k, None)
            data.setdefault("vision", cfg.get("vision", DEFAULTS["vision"]))
            CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
        except Exception:
            log.exception("config migration")
    for role in ("vision", "reasoning"):
        d = dict(DEFAULTS[role]); d.update(cfg.get(role) or {}); cfg[role] = d
    tp = dict(DEFAULTS["toast_position"]); tp.update(cfg.get("toast_position") or {})
    if tp.get("anchor") not in _TOAST_ANCHORS:
        tp["anchor"] = DEFAULTS["toast_position"]["anchor"]
    cfg["toast_position"] = tp
    return cfg


def _spec(d: dict) -> ModelSpec:
    prov, _, model = d["model"].partition(":")
    return ModelSpec(prov, model, d.get("effort", "low"), bool(d.get("thinking", True)))


def _no_markup(s: str) -> str:
    """Escape the HTML subset that markup-capable notification servers parse.

    notify-send bodies are rendered by the server (dunst, mako, GNOME Shell), and servers
    advertising the freedesktop ``body-markup`` capability interpret <b>, <i>, <u>, <a href>
    and <img src> in the text. Our title/body carry model output and screen-derived text, so
    escape the three markup-significant characters and let the server show them literally.
    """
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify(title: str, body: str, urgency: str = "normal"):
    try:
        subprocess.Popen(["notify-send", "-a", "Hermes", "-u", "critical" if urgency == "urgent" else "normal",
                          _no_markup(title), _no_markup(body)])
    except Exception:
        pass


class Companion:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = State(eyes=bool(cfg.get("eyes", True)))
        self.perceiver = Perceiver(cfg["change_threshold"], cfg["max_width"])
        self.policy = SpeechPolicy(PolicyConfig(cfg["min_gap_seconds"], cfg["urgent_gap_seconds"], cfg["max_per_hour"]))
        self.catalog = Catalog()
        self.refresh_catalog()
        # startup guard: the vision model must see images ("" = Hermes' main model)
        vk = cfg["vision"]["model"] or self._hermes_main_key()
        cfg["vision"]["model"] = vk
        if self.catalog.providers and self.catalog.supports_vision(vk) is not True:
            fallback = self.catalog.default_vision_key(prefer=self._hermes_main_key())
            log.warning("vision model %s has no image input; falling back to %s", vk, fallback)
            self.state.toast(f"{vk} cannot see images — vision model set to {fallback}", "held", "startup")
            cfg["vision"]["model"] = fallback
            self._persist_cfg({"vision": cfg["vision"]})
        self.agent = self._build_agent()
        self._publish_models()
        self.state.update(actions=bool(cfg.get("actions")))
        self.state.update(language=cfg.get("language") or "auto")
        self.state.update(user_context=cfg.get("user_context") or "")
        self.state.update(toast_position=cfg["toast_position"])
        self.voice = None
        self.approver = None
        self._stop = threading.Event()
        self._speak_lock = threading.Lock()
        self._stuck_ticks = 0
        self.ctl = ControlServer(self.handle_command)
        self.ctl.start()

    # ------------------------------------------------------------ status
    def set_status(self, s: str):
        if s in ("listening", "watching"):
            s = "watching" if self.state.get("eyes") else "paused"
        self.state.update(status=s)

    # ------------------------------------------------------------ actions
    def _install_approver(self):
        """Route Hermes' dangerous-command gate (child agents included) to toast + voice."""
        from tools import delegate_tool_config
        from tools.terminal_tool import set_approval_callback

        self.approver = Approver(
            toast=lambda text, kind, note: self.state.toast(text, kind, note),
            speak=self.say,
            listen_yes_no=self._listen_yes_no,
            set_status=self.set_status,
        )
        set_approval_callback(self.approver)
        # Subagent worker threads normally get an auto-deny callback (delegation.subagent_auto_approve);
        # in this process the human is reachable through toast + voice, so route them to the approver.
        # delegate_tool_child_run imports the getter from tools.delegate_tool, so patch it there too.
        from tools import delegate_tool
        delegate_tool_config._get_subagent_approval_callback = lambda: self.approver
        delegate_tool._get_subagent_approval_callback = lambda: self.approver
        install_hook()   # tier-5 block / tier-4 escalate for terminal + file tools
        os.environ["HERMES_INTERACTIVE"] = "1"   # tells Hermes' gate a human can answer

    def _listen_yes_no(self, timeout: float):
        if not self.voice:
            return None
        return self.voice.listen_for(timeout, parse_yes_no)

    def set_actions(self, enabled: bool) -> str:
        self.cfg["actions"] = bool(enabled)
        self._persist_cfg({"actions": bool(enabled)})
        new = self._build_agent()
        new.history = list(self.agent.history)
        self.agent = new
        self.state.update(actions=bool(enabled))
        audit({"kind": "actions", "enabled": bool(enabled)})
        return f"actions={'on' if enabled else 'off'}"

    def set_language(self, language: str) -> str:
        language = language.strip() or "auto"
        self.cfg["language"] = language
        self._persist_cfg({"language": language})
        new = self._build_agent()
        new.history = list(self.agent.history)
        self.agent = new
        self.state.update(language=language)
        return f"language={language}"
    def set_user_context(self, user_context: str) -> str:
        user_context = user_context.strip()
        self.cfg["user_context"] = user_context
        self._persist_cfg({"user_context": user_context})
        new = self._build_agent()
        new.history = list(self.agent.history)
        self.agent = new
        self.state.update(user_context=user_context)
        return f"user_context={user_context or '(default)'}"

    # ------------------------------------------------------------ voice
    def start_voice(self):
        try:
            from voice import Voice

            self.voice = Voice(on_request=self.on_voice_request, on_status=self.set_status)
        except Exception as e:  # noqa: BLE001
            log.exception("voice init failed")
            self.state.update(last_error=f"voice: {e}")

    def on_voice_request(self, text: str, source: str = "voice"):
        self.set_status("thinking")
        try:
            reply = self.agent.ask(text, source=source)
        except Exception as e:  # noqa: BLE001
            log.exception("ask")
            reply = "Sorry, I hit an error answering that."
            self.state.update(last_error=str(e))
        self.state.add_remark(f"You: {text}\nHermes: {reply}", "reply")
        self.state.toast(reply, "reply")
        self.say(reply)

    def say(self, text: str):
        if not text:
            return
        with self._speak_lock:
            if self.voice:
                self.voice.speak(text)
            else:
                notify("Hermes", text)
        self.set_status("watching")

    # ------------------------------------------------------------ perception loop
    def tick(self):
        frame = self.perceiver.observe(eyes_enabled=self.state.get("eyes"))
        self.state.update(ticks=self.state.get("ticks", 0) + 1)
        if not self.state.get("eyes"):
            return
        if frame.blocked_by:
            self.state.update(last_observation=f"({frame.blocked_by}) — not looking")
            return
        if frame.idle_seconds > self.cfg.get("idle_skip_seconds", 300):
            return  # user away: no point burning tokens
        if not frame.changed:
            self._stuck_ticks += 1
            # Only consult the model on unchanged screens every ~2 min (possible "stuck" signal)
            if self._stuck_ticks % max(1, int(120 / self.cfg["tick_seconds"])) != 0:
                return
        else:
            self._stuck_ticks = 0

        w = frame.window
        text = (
            f"[SCREEN TICK] {time.strftime('%H:%M')}\n"
            f"window: {w.cls!r} title: {w.title[:160]!r} workspace: {w.workspace} fullscreen: {w.fullscreen}\n"
            f"idle: {int(frame.idle_seconds)}s  unchanged_ticks: {self._stuck_ticks}\n"
            + ("(screenshot attached)" if frame.jpeg else "(screen unchanged since last frame — no screenshot)")
        )
        self.set_status("thinking")
        try:
            res = self.agent.observe(text, frame.data_url())
        except Exception as e:  # noqa: BLE001
            log.exception("observe")
            self.state.update(last_error=str(e))
            self.set_status("watching")
            return
        if frame.jpeg:
            self.state.update(frames_sent=self.state.get("frames_sent", 0) + 1)
        self.state.update(last_observation=res["observation"])
        self.set_status("watching")
        wants = bool(res["should_speak"] and res["text"])
        # Checked after the model turn (it takes seconds): unprompted output must not be drawn
        # into a screen share or recording. Replies to the user's own requests still toast.
        sharing = screen_shared()
        if not wants:
            # Every model output is surfaced as a toast; silent observations are shown dimmed.
            if res["observation"] and not sharing:
                self.state.toast(res["observation"], "observation")
            return
        ok, why = self.policy.may_speak(frame, res["urgency"], self.state.get("muted"))
        log.info("wants to speak (%s): %s -> %s", res["urgency"], res["text"], why)
        kind = "urgent" if res["urgency"] == "urgent" else "remark"
        if ok:
            self.policy.record()
            self.state.add_remark(res["text"], res["urgency"])
            self.state.toast(res["text"], kind)
            if self.cfg.get("notify"):
                notify("Hermes", res["text"], res["urgency"])
            self.say(res["text"])
        elif sharing:
            # Screen is shared/recorded: keep it off-screen, in the panel's recent list.
            self.state.add_remark(res["text"], "held")
        else:
            # Policy blocked the voice; still show it (with the reason) so nothing is lost.
            self.state.toast(res["text"], "held", why)

    def loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.tick()
            except Exception:
                log.exception("tick")
            self._stop.wait(max(2.0, self.cfg["tick_seconds"] - (time.time() - t0)))

    # ------------------------------------------------------------ model selection
    @staticmethod
    def _hermes_main_key() -> str:
        """provider:model from ~/.hermes/config.yaml — the user's primary subscription."""
        try:
            from hermes_cli.config import load_config_readonly

            m = load_config_readonly().get("model") or {}
            if isinstance(m, dict) and m.get("provider") and m.get("default"):
                return f'{m["provider"]}:{m["default"]}'
        except Exception:
            pass
        return ""

    def refresh_catalog(self):
        main = self._hermes_main_key().partition(":")[0] or (self.cfg["vision"]["model"].partition(":")[0]) or "anthropic"
        try:
            self.catalog.refresh(main)
        except Exception:
            log.exception("catalog refresh")

    def _publish_models(self):
        v, r = self.cfg["vision"], self.cfg["reasoning"]
        self.state.update(
            models=self.catalog.to_state(),
            vision=dict(v),
            reasoning=dict(r),
            split=bool(r["model"] and r["model"] != v["model"]),
            # legacy keys some widget code still reads
            model_key=v["model"], model=v["model"].partition(":")[2], effort=v["effort"], thinking=v["thinking"],
        )

    def _build_agent(self) -> CompanionAgent:
        v = _spec(self.cfg["vision"])
        r = _spec(self.cfg["reasoning"]) if self.cfg["reasoning"]["model"] else None
        return CompanionAgent(v, r, self.cfg["user_name"], actions=bool(self.cfg.get("actions")),
                               language=str(self.cfg.get("language") or "auto"),
                               user_context=str(self.cfg.get("user_context") or ""))

    def set_role(self, role: str, model: str | None = None, effort: str | None = None, thinking: bool | None = None) -> str:
        if role not in ("vision", "reasoning"):
            return f"bad role: {role}"
        d = dict(self.cfg[role])
        if model is not None:
            if role == "reasoning" and model.lower() in ("same", "", "none"):
                model = ""
            elif not self.catalog.exists(model):
                return f"error: unknown model {model}"
            elif role == "vision" and self.catalog.supports_vision(model) is False:
                return f"error: {model} has no image input — pick a vision model"
            d["model"] = model
        if effort is not None:
            if effort not in ("low", "medium", "high"):
                return f"bad effort: {effort}"
            d["effort"] = effort
        if thinking is not None:
            d["thinking"] = bool(thinking)
        self.set_status("thinking")
        old = self.cfg[role]
        self.cfg[role] = d
        try:
            new = self._build_agent()
        except Exception as e:  # noqa: BLE001
            self.cfg[role] = old
            log.exception("set_role")
            self.state.update(last_error=f"model switch failed: {e}")
            self.set_status("watching")
            return f"error: {e}"
        # carry the conversation over; don't block on an in-flight turn (may be in a retry backoff).
        new.history = list(self.agent.history)
        self.agent = new
        self._persist_cfg({role: d})
        self.state.update(last_error="")
        self._publish_models()
        self.set_status("watching")
        log.info("%s -> %s", role, d)
        return f"{role}={d['model'] or 'same'} effort={d['effort']} thinking={d['thinking']}"

    def _persist_cfg(self, patch: dict):
        try:
            data = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
            data.update(patch)
            CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
        except Exception:
            log.exception("persist config")

    # ------------------------------------------------------------ control
    def handle_command(self, cmd: str) -> str:
        op, _, arg = cmd.partition(" ")
        op = op.lower()
        arg = arg.strip()
        if op == "status":
            return json.dumps(self.state.data)
        if op == "models":
            self.refresh_catalog()
            self._publish_models()
            return "\n".join(f'{m["value"]}\t{"vision" if m.get("vision") else "text"}' for m in self.catalog.to_state() if not m.get("header"))
        if op in ("set-vision", "set-model") and arg:
            return self.set_role("vision", model=arg)
        if op == "set-reasoning" and arg:
            return self.set_role("reasoning", model=arg)
        if op == "set-vision-effort" and arg:
            return self.set_role("vision", effort=arg)
        if op == "set-reasoning-effort" and arg:
            return self.set_role("reasoning", effort=arg)
        if op == "toggle-vision-thinking":
            return self.set_role("vision", thinking=not self.cfg["vision"]["thinking"])
        if op == "toggle-reasoning-thinking":
            return self.set_role("reasoning", thinking=not self.cfg["reasoning"]["thinking"])
        # legacy aliases (apply to vision)
        if op == "set-effort" and arg:
            return self.set_role("vision", effort=arg)
        if op == "toggle-thinking":
            return self.set_role("vision", thinking=not self.cfg["vision"]["thinking"])
        if op == "toggle-eyes":
            v = not self.state.get("eyes")
            self.state.update(eyes=v)
            self.set_status("watching")
            return f"eyes={'on' if v else 'off'}"
        if op == "listen":
            if not self.voice:
                return "voice not ready"
            return "listening" if self.voice.listen() else "already listening"
        if op == "toggle-mute":
            v = not self.state.get("muted")
            self.state.update(muted=v)
            return f"muted={'on' if v else 'off'}"
        if op == "toggle-actions":
            return self.set_actions(not self.cfg.get("actions"))
        if op == "set-language" and arg:
            return self.set_language(arg)
        if op == "set-user-context":
            return self.set_user_context(arg)
        if op == "decide" and arg:
            from actions import DECISION_FILE
            DECISION_FILE.parent.mkdir(parents=True, exist_ok=True)
            DECISION_FILE.write_text(json.dumps({"approve": arg.lower() in ("yes", "run", "approve", "1", "true")}))
            return "decided"
        if op == "toggle-toasts":
            v = not self.state.get("toasts", True)
            self.state.update(toasts=v)
            return f"toasts={'on' if v else 'off'}"
        if op == "set-toast-position" and arg:
            # "anchor[,margin_x[,margin_y]]" e.g. "top-left,20,10"
            parts = [p.strip() for p in arg.split(",")]
            anchor = parts[0] if parts else ""
            if anchor not in _TOAST_ANCHORS:
                return f"unknown anchor: {anchor!r} (expected one of {', '.join(_TOAST_ANCHORS)})"
            try:
                margin_x = int(parts[1]) if len(parts) > 1 and parts[1] else self.cfg["toast_position"]["margin_x"]
                margin_y = int(parts[2]) if len(parts) > 2 and parts[2] else self.cfg["toast_position"]["margin_y"]
            except ValueError:
                return "margin_x/margin_y must be integers"
            tp = {"anchor": anchor, "margin_x": margin_x, "margin_y": margin_y}
            self.cfg["toast_position"] = tp
            self._persist_cfg({"toast_position": tp})
            self.state.update(toast_position=tp)
            return f"toast_position={anchor} margin=({margin_x},{margin_y})"
        if op == "toast" and arg:
            self.state.toast(arg, "remark")
            return "toasted"
        if op == "hush":
            if self.voice:
                self.voice.hush()
            return "hushed"
        if op == "say" and arg:
            threading.Thread(target=self.say, args=(arg,), daemon=True).start()
            return "speaking"
        if op == "ask" and arg:
            threading.Thread(target=self.on_voice_request, args=(arg,), daemon=True).start()
            return "asking"
        if op == "text" and arg:
            threading.Thread(target=self.on_voice_request, args=(arg, "text"), daemon=True).start()
            return "asking"
        if op == "tick":
            threading.Thread(target=self.tick, daemon=True).start()
            return "ticking"
        if op == "quit":
            self._stop.set()
            os.kill(os.getpid(), signal.SIGTERM)
            return "bye"
        return f"unknown command: {op}"

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        threading.Thread(target=self.start_voice, daemon=True, name="voice-init").start()
        self._install_approver()
        self.set_status("watching")
        log.info("companion running (tick=%ss)", self.cfg["tick_seconds"])
        try:
            self.loop()
        finally:
            self.state.update(status="stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctl", help="send a command to the running daemon and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.ctl:
        from state import send_command

        print(send_command(args.ctl))
        return
    Companion(load_config()).run()


if __name__ == "__main__":
    main()

