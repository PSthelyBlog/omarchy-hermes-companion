"""Long-lived Hermes AIAgent wrapper for the companion.

Two roles:
  * vision   — sees the screenshot. If it is also the reasoning model, frames are
               attached natively to the persistent conversation (best path).
  * reasoning — owns the persistent conversation, decides whether to speak, answers
               voice/text requests. When it differs from the vision model, the
               vision model describes each frame in a stateless one-shot and the
               description is fed to the reasoning conversation as text.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes/hermes-agent"))
sys.path.insert(0, str(HERMES_ROOT))

log = logging.getLogger("companion.agent")

PROMPT_FILE = Path(__file__).with_name("prompt.md")
READ_ONLY_TOOLS = {"web_search", "web_extract", "read_file", "search_files", "vision_analyze"}
ACTION_TOOLS = READ_ONLY_TOOLS | {"delegate_task"}
KEEP_IMAGES = 3  # most recent frames kept in context; older ones are replaced by a stub

DESCRIBE_PROMPT = (
    "You are the eyes of a desktop monitoring assistant. Describe this screenshot for a colleague "
    "who cannot see it, in at most 150 words of plain prose: the application and what the user is doing, "
    "any error messages, stack traces, warnings, dialogs, failing checks, merge conflicts, "
    "meeting/notification banners, and anything a helpful colleague glancing over the shoulder "
    "would point out. If a secret (API key, token, password, private key) is visible, say what kind it is "
    "and where (file, line), but never copy any part of its value. Quote error messages and file:line "
    "verbatim. No preamble, no markdown."
)

REDACTED = "[redacted secret]"
# Backstop for the prompts' "never repeat a secret" rule, applied to everything the models write
# before it reaches the conversation, toasts, state.json, the journal or TTS. Screenshots are not altered.
_SECRET_RE = re.compile(
    r"\b(?:sk-(?:ant-)?[\w-]{16,}|gh[pousr]_\w{20,}|github_pat_\w{20,}|xox[abpr]-[\w-]{10,}"
    r"|AKIA[0-9A-Z]{16}|AIza[\w-]{30,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,})"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\s*[A-Za-z0-9+/=]{40,})*(?:\s*-----END [A-Z ]*PRIVATE KEY-----)?"
)
# `NAME=value` / `name: value` (.env, YAML, JSON, URLs) whose name *ends* in key/token/password…,
# so `KeyError: …` and `MAX_TOKENS=…` are left alone. The name is kept, the value is masked.
_ASSIGNED_SECRET_RE = re.compile(
    r"(\b[\w-]*(?:key|secret|token|password|passwd|pwd|pass)[\"'`]?\s*[=:]\s*[\"'`]?)"
    r"(?!\[redacted)[^\s\"'`,;]{6,}",
    re.I,
)


def _redact(text: str) -> str:
    text = _ASSIGNED_SECRET_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return _SECRET_RE.sub(REDACTED, text)

# Neutral default, used when companion.json sets no "user_context".
_DEFAULT_USER_CONTEXT = "{{USER}} is a developer."


def _user_context(user_context: str) -> str:
    """Free-text description of the user/environment injected near the top of the system
    prompt, so anyone can describe who they are and how they work without editing prompt.md.
    Empty/unset falls back to a neutral default."""
    user_context = (user_context or "").strip()
    return user_context or _DEFAULT_USER_CONTEXT


def _language_rule(language: str) -> str:
    """Reply-language instruction, shared by the system prompt and every per-turn nudge.

    "auto" (default) keeps the companion's spirit language-agnostic: it follows whichever
    language {{USER}} actually used, so nothing has to be configured for the common case
    and nothing here hardcodes a single language. A fixed value pins every reply instead —
    useful when the user always addresses it in a language other than English but the vision
    model's screen narration or transcription occasionally slips into English.
    """
    language = (language or "auto").strip()
    if language.lower() in ("", "auto", "user", "match"):
        return "Reply in the language {{USER}} addressed you in — match their language, don't translate it."
    return "Reply in " + language + ", regardless of the language {{USER}} addressed you in."


@dataclass
class ModelSpec:
    provider: str
    model: str
    effort: str = "low"
    thinking: bool = True

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            return json.loads(m.group(0).split("```")[0])
        except Exception:
            return None


_REQUEST_TAGS = ("[VOICE REQUEST", "[TEXT REQUEST")


def _text_of(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return c if isinstance(c, str) else ""


def _drop_screen_turns(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only voice/text request turns: the user message plus the assistant and tool messages
    after it. Screen ticks (frames, descriptions, window titles, observations) and anything
    unrecognised, such as Hermes' compaction summaries, are dropped. Whole turns go, so a tool
    call is never separated from its result."""
    out, keep = [], False
    for m in history:
        if m.get("role") == "user":
            keep = _text_of(m).lstrip().startswith(_REQUEST_TAGS)
        if keep:
            out.append(m)
    return out


def _make_agent(spec: ModelSpec, user_name: str, system_prompt: str, tools: bool, max_iterations: int, actions: bool = False):
    from run_agent import AIAgent

    # Children inherit the parent's toolsets, so the action agent must carry terminal+file
    # itself; its own schema is then trimmed back to ACTION_TOOLS below.
    toolsets = ["web", "file", "vision"] + (["terminal", "delegation"] if actions else [])
    agent = AIAgent(
        model=spec.model,
        provider=spec.provider,
        enabled_toolsets=toolsets if tools else [],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        reasoning_config={"enabled": bool(spec.thinking), "effort": spec.effort},
        max_iterations=max_iterations,
        platform="companion",
        user_name=user_name,
        ephemeral_system_prompt=system_prompt,
    )
    allowed = ACTION_TOOLS if actions else READ_ONLY_TOOLS
    agent.tools = [t for t in (agent.tools or []) if tools and t["function"]["name"] in allowed]
    agent.valid_tool_names = {t["function"]["name"] for t in agent.tools}
    return agent


class CompanionAgent:
    def __init__(self, vision: ModelSpec, reasoning: Optional[ModelSpec], user_name: str,
                 actions: bool = False, language: str = "auto", user_context: str = ""):
        self.user_name = user_name
        self.language_rule = _language_rule(language).replace("{{USER}}", user_name)
        # {{USER}} inside the resolved user-context text (default or custom) must expand to
        # the real name too, so replace {{USER}} first, then splice in {{USER_CONTEXT}}.
        resolved_context = _user_context(user_context).replace("{{USER}}", user_name)
        self.system_prompt = (PROMPT_FILE.read_text()
                               .replace("{{USER}}", user_name)
                               .replace("{{LANGUAGE_RULE}}", self.language_rule)
                               .replace("{{USER_CONTEXT}}", resolved_context))
        self.history: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.vision = vision
        self.reasoning = reasoning or vision
        self.split = reasoning is not None and reasoning.key != vision.key
        self.actions = actions
        self.agent = _make_agent(self.reasoning, user_name, self.system_prompt, tools=True, max_iterations=6)
        # Request-only twin with delegate_task. Separate instance so screen ticks keep the
        # read-only schema (and their prompt cache); it shares `history` by reference at call time.
        self.actor = _make_agent(self.reasoning, user_name, self.system_prompt, tools=True, max_iterations=10, actions=True) if actions else None
        self.eyes = _make_agent(self.vision, user_name, DESCRIBE_PROMPT, tools=False, max_iterations=1) if self.split else None
        log.info(
            "agent ready: reasoning=%s (effort=%s thinking=%s) vision=%s%s tools=%s",
            self.reasoning.key, self.reasoning.effort, self.reasoning.thinking, self.vision.key,
            " [split]" if self.split else " [native]", sorted(self.agent.valid_tool_names),
        )

    # ------------------------------------------------------------------ helpers
    def _prune_images(self):
        seen = 0
        for msg in reversed(self.history):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            if not any(p.get("type") in ("image_url", "image") for p in msg["content"]):
                continue
            seen += 1
            if seen > KEEP_IMAGES or self.split:
                msg["content"] = [p for p in msg["content"] if p.get("type") == "text"] or [{"type": "text", "text": "[earlier screen frame omitted]"}]
                msg["content"].append({"type": "text", "text": "[screen frame omitted to save context]"})

    def _turn(self, content: Any, agent=None) -> str:
        agent = agent or self.agent
        with self._lock:
            # No later turn consumes a detached delegate_task result here, so declare the
            # session stateless: Hermes then runs children synchronously and the helper's
            # result comes back inside this very turn (ContextVar → must be set per thread).
            try:
                from gateway.session_context import declare_stateless_channel
                declare_stateless_channel()
            except Exception:
                pass
            self._prune_images()
            result = agent.run_conversation(content, system_message=self.system_prompt, conversation_history=self.history)
            self.history = [m for m in (result.get("messages") or self.history) if m.get("role") != "system"]
            return (result.get("final_response") or "").strip()

    def _describe(self, image_data_url: str) -> str:
        """Stateless one-shot on the vision model."""
        parts = [{"type": "text", "text": "Describe this screenshot."}, {"type": "image_url", "image_url": {"url": image_data_url}}]
        result = self.eyes.run_conversation(parts, system_message=DESCRIBE_PROMPT, conversation_history=[])
        # Redacted before it enters the reasoning conversation, where it would be re-sent every turn.
        return _redact((result.get("final_response") or "").strip())

    # ------------------------------------------------------------------ API
    def adopt_history(self, old: "CompanionAgent"):
        """Continue `old`'s conversation after a rebuild (model, actions, language… changed).

        The history is sent to the reasoning provider, so a new one gets only the request
        turns: it must not receive frames, descriptions or titles captured for another."""
        self.history = list(old.history)
        if self.reasoning.provider != old.reasoning.provider:
            kept = _drop_screen_turns(self.history)
            log.info("reasoning provider %s -> %s: screen history not carried over (%d of %d messages kept)",
                     old.reasoning.provider, self.reasoning.provider, len(kept), len(self.history))
            self.history = kept

    def observe(self, text: str, image_data_url: Optional[str]) -> dict:
        """Feed one perception tick. Returns {observation, should_speak, urgency, text}."""
        if image_data_url and self.split:
            try:
                desc = self._describe(image_data_url)
            except Exception:
                log.exception("vision describe failed")
                desc = "(vision model failed to describe the frame)"
            content: Any = text.replace("(screenshot attached)", "") + f"\n[SCREEN DESCRIPTION by {self.vision.model}]\n{desc}"
        elif image_data_url:
            content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": image_data_url}}]
        else:
            content = text
        raw = self._turn(content)
        data = _extract_json(raw) or {}
        return {
            # Redact before truncating, so a key cut at the limit is still recognised.
            "observation": _redact(str(data.get("observation", "")))[:500],
            "should_speak": bool(data.get("should_speak", False)),
            "urgency": str(data.get("urgency", "low")),
            "text": _redact(str(data.get("text", "")).strip()),
        }

    def ask(self, transcript: str, source: str = "voice") -> str:
        """Voice or typed request from the user. Returns plain spoken reply."""
        tag = "VOICE REQUEST" if source == "voice" else "TEXT REQUEST"
        msg = (
            f"[{tag} from {self.user_name}]\n{transcript}\n\n"
            f"Reply in plain spoken prose (no JSON, no markdown, no lists), 1-4 sentences unless "
            f"more is truly needed. {self.language_rule}"
        )
        return _redact(self._turn(msg, agent=self.actor if self.actions else None))
