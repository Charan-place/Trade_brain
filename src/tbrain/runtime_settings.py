"""Dashboard switches the brain picks up while running (no restart, no redeploy).

Only whitelisted booleans are stored (see SignalEngine.RUNTIME_KEYS):
  allow_afternoon  - keep issuing calls after the 13:00 cutoff (default off)
  dd_stop          - drawdown stop on today's closed calls (default on)
Local:  data/live/settings.json.   Modal: the shared modal.Dict (key "settings").
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

ALLOWED = ("allow_afternoon", "dd_stop")


def clean(d: dict | None) -> dict:
    return {k: v for k, v in (d or {}).items() if k in ALLOWED and isinstance(v, bool)}


class _Store:
    """Shared doc: dashboard switches (whitelisted booleans) + owner-only real-money control + a command queue.
    Subclasses implement _load() / _save(doc)."""

    def _load(self) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def _save(self, doc: dict) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def get(self) -> dict:
        return clean(self._load())

    def update(self, changes: dict) -> dict:
        doc = self._load()
        doc.update(clean(changes))
        self._save(doc)
        return clean(doc)

    # ---- real money (owner only; validated by the web layer before it gets here)
    def get_real(self) -> dict:
        r = self._load().get("_real") or {}
        return {"on": bool(r.get("on")), "loss_cap": r.get("loss_cap"), "changed_ms": r.get("changed_ms"),
                "why_off": r.get("why_off")}

    def set_real(self, **fields) -> dict:
        doc = self._load()
        r = dict(doc.get("_real") or {})
        r.update({k: v for k, v in fields.items() if k in ("on", "loss_cap", "why_off")})
        r["changed_ms"] = time.time() * 1000
        doc["_real"] = r
        self._save(doc)
        return self.get_real()

    # ---- commands (exit one / exit all / approve), processed once each by the execution hub
    def push_command(self, kind: str, call_id: str | None = None) -> dict:
        doc = self._load()
        cmds = [c for c in (doc.get("_cmds") or []) if time.time() * 1000 - c.get("ms", 0) < 3_600_000][-50:]
        c = {"id": uuid.uuid4().hex[:12], "kind": kind, "call_id": call_id, "ms": time.time() * 1000}
        cmds.append(c)
        doc["_cmds"] = cmds
        self._save(doc)
        return c

    def commands(self) -> list[dict]:
        return list(self._load().get("_cmds") or [])


class FileSettings(_Store):
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, self.path)


class DictSettings(_Store):
    def __init__(self, d, key: str = "settings"):
        self.d, self.key = d, key

    def _load(self) -> dict:
        try:
            d = self.d.get(self.key)
            return dict(d) if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save(self, doc: dict) -> None:
        self.d[self.key] = doc
