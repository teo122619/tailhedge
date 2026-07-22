"""Shared filesystem helper for the CLIs.

Provides `default_runs_dir`, the fallback `./runs` output directory used by
`advisor_cli.py` and `hedge_cli.py` when `--out-dir` is not given, so every
report-writing entry point saves to the same place by default.
"""

from __future__ import annotations

from pathlib import Path


def default_runs_dir() -> Path:
    """Default output directory for run reports: ./runs under the current
    working directory. CLIs override it with --out-dir. Created lazily by
    the caller (mkdir at write time), never here."""
    return Path.cwd() / "runs"
