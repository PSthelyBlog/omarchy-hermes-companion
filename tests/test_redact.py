"""brain._redact: secret values are masked, ordinary screen text is not.

Fake tokens are assembled at runtime so no literal in this file looks like a real credential
to secret scanners."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

from brain import REDACTED, _redact  # noqa: E402

ANTHROPIC = "sk-" + "ant-api03-" + "Xy7" * 14
OPENAI = "sk-" + "proj" + "Ab1" * 12
GITHUB = "gh" + "p_" + "Q9z" * 12
GITHUB_PAT = "github" + "_pat_" + "11AB" * 8
SLACK = "xo" + "xb-" + "1234-5678-" + "aBc" * 6
AWS = "AK" + "IA" + "Z7Q2" * 4
GOOGLE = "AI" + "za" + "Sy" + "k3J" * 11
JWT = "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "ey" + "JzdWIiOiIxMjM0NSJ9" + "." + "s1gn4tur3" * 3
PEM = ("-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + ("b3BlbnNzaC1rZXktdjEAAAAA" * 3 + "\n") * 3
       + "-----END " + "OPENSSH PRIVATE KEY-----")


class KnownFormats(unittest.TestCase):
    def test_each_format_is_masked_and_context_kept(self):
        for token in (ANTHROPIC, OPENAI, GITHUB, GITHUB_PAT, SLACK, AWS, GOOGLE, JWT):
            with self.subTest(token=token[:6]):
                out = _redact(f"An exposed key {token} is visible on line 3 of config.py.")
                self.assertNotIn(token, out)
                self.assertEqual(out, f"An exposed key {REDACTED} is visible on line 3 of config.py.")

    def test_private_key_block_is_masked_whole(self):
        out = _redact(f"id_ed25519 is open:\n{PEM}\nand the user is scrolling.")
        self.assertEqual(out, f"id_ed25519 is open:\n{REDACTED}\nand the user is scrolling.")


class Assignments(unittest.TestCase):
    def test_env_yaml_json_url(self):
        cases = {
            "DB_PASSWORD=hunter22": f"DB_PASSWORD={REDACTED}",
            "AWS_SECRET_ACCESS_KEY = wJalrXUtnFEMI": f"AWS_SECRET_ACCESS_KEY = {REDACTED}",
            "password: correcthorse": f"password: {REDACTED}",
            '"api_key": "abc123def456"': f'"api_key": "{REDACTED}"',
            # `&` stays in the value on purpose: a password containing one must not leak its tail.
            "https://x.test/cb?token=a1b2c3d4e5&next=/": f"https://x.test/cb?token={REDACTED}",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(_redact(src), want)

    def test_known_format_inside_assignment_is_masked_once(self):
        self.assertEqual(_redact(f"ANTHROPIC_API_KEY={ANTHROPIC}"), f"ANTHROPIC_API_KEY={REDACTED}")


class LeftAlone(unittest.TestCase):
    def test_ordinary_screen_text(self):
        for text in (
            "Traceback in src/auth/token.py:42: KeyError: 'username_missing'",
            "MAX_TOKENS=100000 in settings.py:12",
            "The task-scheduler-component-v2 build is failing on risk-assessment-pipeline.",
            "Commit 3f9a2c1e8b7d6f5a4c3b2a1908f7e6d5c4b3a291 was force-pushed.",
            "passport.js:10 exports a strategy; password field is empty.",
            "The user is editing .env in Neovim; an Anthropic API key is visible on line 3.",
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(_redact(text), text)

    def test_idempotent(self):
        once = _redact(f"API_KEY={ANTHROPIC} and {GITHUB}")
        self.assertEqual(_redact(once), once)


if __name__ == "__main__":
    unittest.main()
