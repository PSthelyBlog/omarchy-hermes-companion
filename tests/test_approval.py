"""actions.Approver / actions.decide: an answer counts only for the approval request it names.

Hermes is not needed: classify() is stubbed and the voice listener is a fake."""
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import actions  # noqa: E402

TIMEOUT = 1.0  # seconds an approval request waits in these tests


def until(cond, limit=3.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not reached")


class Harness:
    """An Approver wired to fakes. `voice` plans one answer per request: None for silence,
    or (delay, True/False)."""

    def __init__(self, voice=()):
        self.published, self.toasts, self.cancels = [], [], []
        self.voice = list(voice)
        self.approver = actions.Approver(toast=self.toast, speak=lambda text: None, listen_yes_no=self.listen,
                                         set_status=lambda status: None, publish=self.published.append)

    def toast(self, text, kind, note="", request=""):
        self.toasts.append({"text": text, "kind": kind, "request": request})

    def listen(self, timeout, cancel):
        self.cancels.append(cancel)
        plan = self.voice.pop(0) if self.voice else None
        if plan is None:
            cancel.wait(timeout)
            return None
        delay, answer = plan
        return None if cancel.wait(delay) else answer

    @property
    def pending(self):
        """What the daemon's state holds as `approval` now."""
        return self.published[-1] if self.published else None

    def request(self, command):
        """Start an approval request; return (its id, a function that waits for its verdict)."""
        before = len(self.published)
        verdict = {}
        thread = threading.Thread(target=lambda: verdict.setdefault("v", self.approver(command, "test")), daemon=True)
        thread.start()
        until(lambda: len(self.published) > before and self.pending)
        request = self.pending["id"]

        def result():
            thread.join(TIMEOUT + 2)
            return verdict["v"]
        return request, result


class ApprovalBinding(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.decision_file = Path(tmp.name) / "decision.json"
        for name, value in (("DECISION_FILE", self.decision_file), ("APPROVAL_TIMEOUT", TIMEOUT),
                            ("classify", lambda command: (4, "test")), ("audit", lambda entry: None)):
            patcher = mock.patch.object(actions, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_click_answers_its_own_request(self):
        h = Harness()
        request, result = h.request("rm -r ~/build")
        self.assertEqual(h.toasts[-1], {"text": "rm -r ~/build", "kind": "approval", "request": request})
        self.assertEqual(actions.decide(f"{request} yes", h.pending), "decided")
        self.assertEqual(result(), "once")
        self.assertIsNone(h.pending)

    def test_skip_denies(self):
        h = Harness()
        request, result = h.request("rm -r ~/build")
        actions.decide(f"{request} no", h.pending)
        self.assertEqual(result(), "deny")

    def test_late_answer_to_timed_out_request_does_not_approve_the_next(self):
        h = Harness()
        a, result_a = h.request("command A")
        self.assertEqual(result_a(), "deny")          # silence: A timed out
        b, result_b = h.request("command B")
        self.assertNotEqual(a, b)
        self.assertTrue(actions.decide(f"{a} yes", h.pending).startswith("stale"))   # A's card clicked late
        self.assertFalse(self.decision_file.exists())
        self.assertEqual(result_b(), "deny")          # B still needs its own answer

    def test_late_answer_to_voice_answered_request_does_not_approve_the_next(self):
        h = Harness(voice=[(0.1, True), None])
        a, result_a = h.request("command A")
        self.assertEqual(result_a(), "once")          # A answered by voice; its card is still up
        b, result_b = h.request("command B")
        self.assertTrue(actions.decide(f"{a} yes", h.pending).startswith("stale"))
        self.assertEqual(result_b(), "deny")

    def test_answer_naming_another_request_is_ignored(self):
        # One that slipped past decide(): A settled between the id check and the write.
        h = Harness()
        b, result_b = h.request("command B")
        self.decision_file.write_text(json.dumps({"request": "0123456789abcdef", "approve": True}))
        self.assertEqual(result_b(), "deny")

    def test_answer_without_request_is_ignored(self):
        # The pre-0.6.1 format: a bare boolean that would answer whatever was waiting.
        h = Harness()
        b, result_b = h.request("command B")
        self.decision_file.write_text(json.dumps({"approve": True}))
        self.assertEqual(result_b(), "deny")

    def test_settling_stops_the_voice_listener(self):
        h = Harness()
        request, result = h.request("command A")
        until(lambda: h.cancels)
        self.assertFalse(h.cancels[0].is_set())
        actions.decide(f"{request} yes", h.pending)
        self.assertEqual(result(), "once")
        self.assertTrue(h.cancels[0].is_set())

    def test_decide_needs_a_request_id(self):
        h = Harness()
        request, result = h.request("command A")
        for arg in ("yes", "run", f"{request}", f"{request} yes now"):
            with self.subTest(arg=arg):
                self.assertTrue(actions.decide(arg, h.pending).startswith("usage"))
        self.assertTrue(actions.decide("0123456789abcdef yes", h.pending).startswith("stale"))
        self.assertTrue(actions.decide(f"{request} yes", None).startswith("stale"))
        self.assertEqual(result(), "deny")


class TierFourAsksAPerson(unittest.TestCase):
    """pre_tool_call defers a tier-4 command to Hermes's own gate only when that gate will ask a
    person (Hermes flags it and approvals.mode is manual); otherwise it escalates to the plugin
    gate, which always asks. In smart mode Hermes's gate may let a guardian model approve."""

    def hook(self, hermes_flags: bool, mode: str):
        detection = types.ModuleType("tools.approval_detection")
        detection.detect_dangerous_command = lambda command: (hermes_flags, "flagged", "flagged")
        tools = types.ModuleType("tools")
        tools.approval_detection = detection
        with mock.patch.dict(sys.modules, {"tools": tools, "tools.approval_detection": detection}), \
                mock.patch.object(actions, "classify", lambda command: (4, "dotfile")), \
                mock.patch.object(actions, "_hermes_approval_mode", lambda: mode):
            return actions.pre_tool_call("terminal", {"command": "cp /etc/hostname ~/.x"})

    def test_escalates_unless_hermes_will_ask(self):
        for flags in (True, False):
            for mode in ("smart", "manual", "off", ""):
                with self.subTest(hermes_flags=flags, mode=mode):
                    directive = self.hook(flags, mode)
                    if flags and mode == "manual":
                        self.assertIsNone(directive)   # Hermes's gate asks the Approver itself
                    else:
                        self.assertEqual(directive["action"], "approve")
                        self.assertEqual(directive["rule_key"], "companion")
                        self.assertIn("cp /etc/hostname ~/.x", directive["message"])


if __name__ == "__main__":
    unittest.main()
