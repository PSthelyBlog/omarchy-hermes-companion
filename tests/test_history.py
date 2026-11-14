"""Conversation history: screen turns are dropped on a provider change, forget() wins over a turn in flight.

CompanionAgent is built with __new__ so no Hermes agent (and no network) is needed."""
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import brain  # noqa: E402
from actions import REQUEST, SCREEN  # noqa: E402
from brain import CompanionAgent, ModelSpec, _bound_history, _drop_screen_turns  # noqa: E402

FRAME = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}


def tick_native(n):
    return [
        {"role": "user", "content": [{"type": "text", "text": f"[SCREEN TICK] 10:0{n}\nwindow: 'kitty'"}, FRAME]},
        {"role": "assistant", "content": '{"observation": "terminal", "should_speak": false}'},
    ]


def tick_split(n):
    return [
        {"role": "user", "content": f"[SCREEN TICK] 11:0{n}\n[SCREEN DESCRIPTION by haiku]\nA .env file is open."},
        {"role": "assistant", "content": '{"observation": "editing .env", "should_speak": false}'},
    ]


def request_with_tool(kind, q):
    return [
        {"role": "user", "content": f"[{kind} REQUEST from Philippe]\n{q}"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "results"},
        {"role": "assistant", "content": "Here is what I found."},
    ]


SUMMARY = [{"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns: the user edited .env …"},
           {"role": "assistant", "content": "ok"}]


def agent(provider="anthropic", history=None):
    a = CompanionAgent.__new__(CompanionAgent)
    a.reasoning = ModelSpec(provider, "m")
    a.history = list(history or [])
    a.split = False
    a.system_prompt = ""
    a._lock, a._hist_lock, a._epoch = threading.Lock(), threading.Lock(), 0
    return a


class DropScreenTurns(unittest.TestCase):
    def test_keeps_only_request_turns_whole_and_in_order(self):
        voice = [{"role": "user", "content": "[VOICE REQUEST from Philippe]\nwhat time is it"},
                 {"role": "assistant", "content": "Ten past four."}]
        text = request_with_tool("TEXT", "look up the error")
        history = SUMMARY + tick_native(1) + voice + tick_split(2) + text + tick_native(3)
        self.assertEqual(_drop_screen_turns(history), voice + text)

    def test_nothing_screen_derived_survives(self):
        kept = _drop_screen_turns(SUMMARY + tick_native(1) + tick_split(2))
        self.assertEqual(kept, [])

    def test_unknown_user_messages_are_dropped(self):
        history = [{"role": "user", "content": "[SOMETHING NEW] hello"}, {"role": "assistant", "content": "hi"}]
        self.assertEqual(_drop_screen_turns(history), [])


class AdoptHistory(unittest.TestCase):
    def setUp(self):
        self.history = tick_split(1) + request_with_tool("VOICE", "hi") + tick_native(2)

    def test_same_provider_keeps_everything_as_a_copy(self):
        old, new = agent("anthropic", self.history), agent("anthropic")
        new.adopt_history(old)
        self.assertEqual(new.history, self.history)
        self.assertIsNot(new.history, old.history)

    def test_new_provider_gets_request_turns_only(self):
        old, new = agent("anthropic", self.history), agent("openrouter")
        new.adopt_history(old)
        self.assertEqual(new.history, request_with_tool("VOICE", "hi"))
        self.assertEqual(old.history, self.history)  # the old agent is left alone


class Forget(unittest.TestCase):
    def _hermes(self, during=None):
        class Fake:
            def run_conversation(_, content, system_message, conversation_history):
                if during:
                    during()
                return {"messages": conversation_history + [{"role": "user", "content": content},
                                                            {"role": "assistant", "content": "reply"}],
                        "final_response": "reply"}
        return Fake()

    def test_turn_is_stored_normally(self):
        a = agent()
        self.assertEqual(a._turn("[TEXT REQUEST from P]\nhi", REQUEST, agent=self._hermes()), "reply")
        self.assertEqual(len(a.history), 2)

    def test_forget_clears_history(self):
        a = agent(history=tick_split(1))
        a.forget()
        self.assertEqual(a.history, [])

    def test_forget_during_a_turn_discards_that_turn(self):
        a = agent(history=tick_split(1))
        out = a._turn("[SCREEN TICK] 12:00", SCREEN, agent=self._hermes(during=a.forget))
        self.assertEqual(out, "")
        self.assertEqual(a.history, [])
        # The next turn starts from the empty history.
        a._turn("[TEXT REQUEST from P]\nhi", REQUEST, agent=self._hermes())
        self.assertEqual([m["content"] for m in a.history], ["[TEXT REQUEST from P]\nhi", "reply"])


class HistoryStaysBounded(unittest.TestCase):
    """The companion trims its own history; Hermes's compression (an auxiliary model call) stays off."""

    def setUp(self):
        for name, value in (("MAX_SCREEN_TURNS", 8), ("HISTORY_BUDGET", 4_000), ("IMAGE_CHARS", 600)):
            patcher = mock.patch.object(brain, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def turns(history):
        return brain._split_turns(history)

    def assertWhole(self, history):
        self.assertEqual(history[0]["role"], "user")
        for i, m in enumerate(history):
            for call in m.get("tool_calls") or []:
                self.assertIn(call["id"], {r.get("tool_call_id") for r in history[i + 1:] if r.get("role") == "tool"})
            if m.get("role") == "tool":
                self.assertTrue(any(c["id"] == m["tool_call_id"] for prior in history[:i] for c in prior.get("tool_calls") or []))

    def test_under_the_limits_nothing_changes(self):
        history = tick_split(1) + request_with_tool("VOICE", "q") + tick_split(2)
        self.assertIs(_bound_history(history), history)

    def test_old_screen_turns_go_first_and_in_one_step(self):
        requests = request_with_tool("VOICE", "remind me when the build finishes")
        history = tick_split(0) + requests + [m for n in range(1, 10) for m in tick_split(n)]   # 10 screen turns
        kept = _bound_history(history)
        screen = [t for t in self.turns(kept) if not brain._is_request_turn(t)]
        self.assertEqual(len(screen), 6)                          # cut to TRIM_TO (75 %) of 8, not to 8
        self.assertEqual(screen[-1], tick_split(9))               # the newest are kept, in order
        self.assertTrue(all(m in kept for m in requests))         # the request turn survives
        self.assertWhole(kept)
        self.assertIs(_bound_history(kept), kept)                 # and the next turn leaves it alone

    def test_over_budget_requests_go_after_screen_turns_but_never_the_newest(self):
        big = [request_with_tool("TEXT", f"q{n}") for n in range(3)]
        for turn in big:
            turn[2]["content"] = "x" * 1500                         # a large tool result
        history = big[0] + tick_split(1) + big[1] + tick_split(2) + big[2]
        kept = _bound_history(history)

        def has(m):   # identity: request turns share equal assistant messages
            return any(k is m for k in kept)
        self.assertFalse(any(has(m) for m in history if "SCREEN TICK" in str(m.get("content"))))  # screen turns first
        self.assertFalse(any(has(m) for m in big[0]))             # then the oldest request
        self.assertTrue(all(has(m) for m in big[2]))              # never the newest turn
        self.assertLessEqual(sum(brain._size(m) for m in kept), 4_000)
        self.assertWhole(kept)

    def test_frames_count_toward_the_budget(self):
        history = [m for n in range(7) for m in tick_native(n)]  # 7 frames x 600 > 4000
        kept = _bound_history(history)
        self.assertLess(len(kept), len(history))
        self.assertEqual(kept[-2:], tick_native(6))

    def test_every_turn_is_bounded(self):
        a = agent(history=[m for n in range(10) for m in tick_split(n)])
        a._turn("[SCREEN TICK] 12:00", SCREEN, agent=Forget._hermes(self))
        self.assertLess(len(brain._split_turns(a.history)), 10)


class AgentsHaveNoImageToolsOrCompression(unittest.TestCase):
    """_make_agent asks Hermes for no vision toolset and turns its context compression off."""

    def test_make_agent(self):
        made = []

        class FakeAIAgent:
            def __init__(self, **kw):
                made.append(kw)
                names = ["web_search", "web_extract", "read_file", "search_files", "write_file", "patch",
                         "vision_analyze", "terminal", "delegate_task"]
                self.tools = [{"function": {"name": n}} for n in names]
                self.compression_enabled = True
                self.context_compressor = types.SimpleNamespace(_micro_compact_enabled=True)

        fake = types.ModuleType("run_agent")
        fake.AIAgent = FakeAIAgent
        with mock.patch.dict(sys.modules, {"run_agent": fake}):
            for actions in (False, True):
                with self.subTest(actions=actions):
                    a = brain._make_agent(ModelSpec("anthropic", "m"), "P", "", tools=True, max_iterations=2, actions=actions)
                    self.assertNotIn("vision", made[-1]["enabled_toolsets"])
                    self.assertNotIn("vision_analyze", a.valid_tool_names)
                    self.assertFalse(a.compression_enabled)
                    self.assertFalse(a.context_compressor._micro_compact_enabled)


if __name__ == "__main__":
    unittest.main()
