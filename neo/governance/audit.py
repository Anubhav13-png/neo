"""Append-only, hash-chained audit log: who asked what, what data was touched, what was answered.

Each line carries the hash of the previous line, so editing or deleting any past entry breaks the chain
and `verify()` reports where. Ship the file to WORM storage or a SIEM for retention.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Tuple

from ..util import now_iso, sha, stable_json

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()

    def _last_hash(self) -> str:
        if not os.path.exists(self.path):
            return GENESIS
        last = GENESIS
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def append(self, event: str, actor: str, **data) -> str:
        with self._lock:
            prev = self._last_hash()
            entry = {"time": now_iso(), "event": event, "actor": actor, "data": data, "prev": prev}
            entry["hash"] = sha(prev + stable_json({k: v for k, v in entry.items() if k != "hash"}))
            with open(self.path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            return entry["hash"]

    def verify(self) -> Tuple[bool, int, str]:
        """(intact, entries checked, message)."""
        if not os.path.exists(self.path):
            return True, 0, "no audit log yet"
        prev, n = GENESIS, 0
        with open(self.path) as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                entry = json.loads(line)
                expect = sha(prev + stable_json({k: v for k, v in entry.items() if k != "hash"}))
                if entry.get("prev") != prev or entry.get("hash") != expect:
                    return False, n, f"chain broken at line {lineno}"
                prev, n = entry["hash"], n + 1
        return True, n, f"{n} entries, chain intact"
