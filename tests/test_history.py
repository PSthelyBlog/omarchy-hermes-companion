"""Conversation history: screen turns are dropped on a provider change, forget() wins over a turn in flight.

CompanionAgent is built with __new__ so no Hermes agent (and no network) is needed."""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

from brain import CompanionAgent, ModelSpec, _drop_screen_turns  # noqa: E402

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
        self.assertEqual(a._turn("[TEXT REQUEST from P]\nhi", agent=self._hermes()), "reply")
        self.assertEqual(len(a.history), 2)

    def test_forget_clears_history(self):
        a = agent(history=tick_split(1))
        a.forget()
        self.assertEqual(a.history, [])

    def test_forget_during_a_turn_discards_that_turn(self):
        a = agent(history=tick_split(1))
        out = a._turn("[SCREEN TICK] 12:00", agent=self._hermes(during=a.forget))
        self.assertEqual(out, "")
        self.assertEqual(a.history, [])
        # The next turn starts from the empty history.
        a._turn("[TEXT REQUEST from P]\nhi", agent=self._hermes())
        self.assertEqual([m["content"] for m in a.history], ["[TEXT REQUEST from P]\nhi", "reply"])


if __name__ == "__main__":
    unittest.main()
