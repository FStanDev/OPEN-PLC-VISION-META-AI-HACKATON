"""Load ``.env`` files without a dependency (shared by the CLIs)."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv() -> None:
    """Set missing env vars from ``.env`` in cwd or the project root.

    Strips `` #`` inline comments on unquoted values, keeps ``#`` inside
    quotes, and never overwrites variables that are already set.
    """
    for directory in (Path.cwd(), Path(__file__).resolve().parent.parent):
        env_file = directory / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not value.startswith(("'", '"')):  # unquoted: strip inline comments
                value, _, _ = value.partition(" #")
                value = value.rstrip()
            else:
                value = value.strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
