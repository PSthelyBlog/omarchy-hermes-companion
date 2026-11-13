#!/usr/bin/env python3
"""Run the unit tests on the Python and packages Hermes runs the daemon on, so the tests that go
through Hermes itself (test_tool_policy.ThroughHermes) run instead of being skipped:

    python3 tests/run_in_hermes.py [test names…]
"""
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent")))
import run_agent  # noqa: E402,F401  Hermes's bootstrap re-executes this script on Hermes's Python

sys.path[:0] = [str(HERE.parent / "daemon"), str(HERE)]
names = sys.argv[1:]
suite = (unittest.defaultTestLoader.loadTestsFromNames(names) if names
         else unittest.defaultTestLoader.discover(str(HERE), top_level_dir=str(HERE)))
sys.exit(not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())
