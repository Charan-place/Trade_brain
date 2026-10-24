"""Dashboard switches the brain picks up while running (no restart, no redeploy).

Only whitelisted booleans are stored (see SignalEngine.RUNTIME_KEYS):
  allow_afternoon  - keep issuing calls after the 13:00 cutoff (default off)
  dd_stop          - drawdown stop on today's closed calls (default on)
Local:  data/live/settings.json.   Modal: the shared modal.Dict (key "settings").
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ALLOWED = ("allow_afternoon", "dd_stop")


def clean(d: dict | None) -> dict:
    return {k: v for k, v in (d or {}).items() if k in ALLOWED and isinstance(v, bool)}


class FileSettings:
    def __init__(self, path: Path):
        self.path = Path(path)

    def get(self) -> dict:
        try:
            return clean(json.loads(self.path.read_text()))
        except (OSError, json.JSONDecodeError):
            return {}

    def update(self, changes: dict) -> dict:
        cur = self.get()
        cur.update(clean(changes))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur))
        os.replace(tmp, self.path)
        return cur


class DictSettings:
    def __init__(self, d, key: str = "settings"):
        self.d, self.key = d, key

    def get(self) -> dict:
        try:
            return clean(self.d.get(self.key))
        except Exception:  # noqa: BLE001
            return {}

    def update(self, changes: dict) -> dict:
        cur = self.get()
        cur.update(clean(changes))
        self.d[self.key] = cur
        return cur
