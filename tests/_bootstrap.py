"""Zero setup import path for the test suites.

A fresh clone has neither PYTHONPATH nor a HERMES_HOME, so each suite calls this
first and it sorts both out: the Hermes tree goes on `sys.path`, and a throwaway
home keeps a test run away from real plugin data.

Resolution order for the Hermes tree:
  1. ``HERMES_AGENT_ROOT``
  2. ``~/.hermes/hermes-agent``
  3. the parent of the ``hermes`` package on PATH

A missing tree is a clear message, not an ImportError from three frames down.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

HOME_MARKER = "hermes_cli"


def _is_hermes_root(path: Path) -> bool:
    return (path / HOME_MARKER).is_dir()


def hermes_root() -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("HERMES_AGENT_ROOT")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / ".hermes" / "hermes-agent")
    found = shutil.which("hermes")
    if found:
        resolved = Path(found).resolve()
        for parent in resolved.parents:
            candidates.append(parent)
            if parent.name in ("bin", "Scripts"):
                candidates.append(parent.parent)
    for candidate in candidates:
        try:
            if _is_hermes_root(candidate.resolve()):
                return candidate.resolve()
        except OSError:
            continue
    return None


def sandbox_home() -> Path:
    """A HERMES_HOME this run is allowed to write to, created once per tree."""
    home = Path(__file__).resolve().parent / ".tmp" / "home"
    for sub in ("plugin-data", "sessions", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def ensure() -> Path | None:
    root = hermes_root()
    if root is None:
        sys.stderr.write(
            "Hermes tree not found. Set HERMES_AGENT_ROOT to the directory that\n"
            "contains hermes_cli, or run these suites with the Hermes venv python:\n"
            "  HERMES_AGENT_ROOT=/path/to/hermes-agent python3 tests/test_hooks.py\n")
        return None
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    if not os.environ.get("HERMES_HOME"):
        os.environ["HERMES_HOME"] = str(sandbox_home())
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return root


ensure()
