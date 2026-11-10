"""perception.screen_blocker: private shell overlays block a frame only while they show something.

hyprctl and omarchy-shell are faked with the JSON they print on Omarchy 4.5."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import perception  # noqa: E402

MON = {"id": 0, "name": "HDMI-A-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1,
       "activeWorkspace": {"id": 1}}


def layer(namespace):
    return {"address": "0x1", "x": 1521, "y": 0, "w": 399, "h": 1080, "namespace": namespace, "pid": 1314}


def probe(toasts, displays=("HDMI-A-1",)):
    return {"toasts": toasts, "doNotDisturb": False, "expanded": False, "keyboardMode": False,
            "displays": ["HDMI-A-1", "DP-1"], "focusedDisplay": "HDMI-A-1", "targetDisplay": displays[0] if displays else "",
            "notificationDisplays": list(displays), "surfaces": []}


def fake(namespaces=("omarchy-notifications",), clients=(), shell=None):
    """A _run stand-in: `namespaces` mapped on MON, `shell` the probe's stdout (bytes) or an exception."""
    calls = []

    def run(cmd, timeout=5.0):
        calls.append(cmd)
        if cmd[:2] == ["hyprctl", "clients"]:
            return json.dumps(list(clients)).encode()
        if cmd[:2] == ["hyprctl", "layers"]:
            return json.dumps({"HDMI-A-1": {"levels": {"3": [layer(n) for n in namespaces]}}}).encode()
        if cmd == ["omarchy-shell", "notifications", "probe"]:
            if isinstance(shell, Exception):
                raise shell
            return shell
        raise AssertionError(f"unexpected command {cmd}")

    return run, calls


def blocker(**kw):
    run, calls = fake(**kw)
    with mock.patch.object(perception, "_run", run):
        return perception.screen_blocker(MON), calls


class NotificationCanvas(unittest.TestCase):
    def test_idle_canvas_does_not_block(self):
        # Omarchy 4.5 keeps the canvas mapped with no card on it.
        self.assertEqual(blocker(shell=json.dumps(probe(0)).encode())[0], "")

    def test_card_on_this_display_blocks(self):
        self.assertEqual(blocker(shell=json.dumps(probe(1)).encode())[0], "notification popup")

    def test_card_on_another_display_does_not_block(self):
        self.assertEqual(blocker(shell=json.dumps(probe(2, displays=("DP-1",))).encode())[0], "")

    def test_shell_without_probe_fails_closed(self):
        # Older Omarchy: no probe, and its canvas is mapped only while a card is up.
        self.assertEqual(blocker(shell=b"Function not found.\n")[0], "notification popup")

    def test_unanswered_probe_fails_closed(self):
        for shell in (b"", b"{}", subprocess.TimeoutExpired("omarchy-shell", 2)):
            with self.subTest(shell=shell):
                self.assertEqual(blocker(shell=shell)[0], "notification popup")

    def test_unmapped_canvas_needs_no_probe(self):
        # Under a fullscreen window the canvas steps aside (unmaps) until a card arrives.
        reason, calls = blocker(namespaces=())
        self.assertEqual(reason, "")
        self.assertNotIn(["omarchy-shell", "notifications", "probe"], calls)


class OtherOverlays(unittest.TestCase):
    def test_open_overlay_blocks_beside_idle_canvas(self):
        reason, _ = blocker(namespaces=("omarchy-notifications", "omarchy-clipboard"),
                            shell=json.dumps(probe(0)).encode())
        self.assertEqual(reason, "clipboard history")

    def test_each_overlay_blocks(self):
        for namespace, label in perception.PRIVATE_LAYERS.items():
            if namespace == "omarchy-notifications":
                continue
            with self.subTest(namespace=namespace):
                self.assertEqual(blocker(namespaces=(namespace,))[0], label)

    def test_sensitive_window_still_wins(self):
        keepass = {"mapped": True, "hidden": False, "workspace": {"id": 1}, "at": [0, 0], "size": [800, 600],
                   "class": "org.keepassxc.KeePassXC", "title": "Passwords"}
        reason, _ = blocker(clients=[keepass], shell=json.dumps(probe(0)).encode())
        self.assertEqual(reason, "private window: org.keepassxc.KeePassXC")


if __name__ == "__main__":
    unittest.main()
