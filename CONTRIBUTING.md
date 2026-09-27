# Contributing

Thanks for helping. This plugin is installed straight from `main` by `omarchy plugin add/update`, so every merge is a release for everyone using it. A few rules keep that safe:

1. **Open an issue first** for anything bigger than a bug fix, so we agree on the approach before you spend time on it.
2. **One change per PR.** Small, focused PRs get reviewed and merged fast; bundles wait.
3. **Keep defaults backward-compatible.** New settings must leave existing installs behaving exactly as before unless the user opts in (`companion.json` defaults, toast placement, prompt wording…).
4. **Say how you tested.** State what you ran live on Omarchy (voice request, screen tick, restart, multi-monitor…) and what you saw. Untested QML/daemon changes are not merged.
5. **Don't touch the trust boundary casually**: `install.sh`, the systemd unit, `requirements.lock`, network calls, or the read-only tool set in `daemon/brain.py`. Changes there need an explicit explanation in the PR.

CI only checks that the Python compiles, the JSON parses and the unit tests in `tests/` pass; the real review is a human reading the diff and running it. AI-assisted PRs are welcome — the same rules apply.

Layout of the code is in the README under *Layout*; `--ctl` commands under *Commands*.
