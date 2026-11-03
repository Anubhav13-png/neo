"""The catalog: which tables exist, which versions they have, and what a snapshot pins.

Every question pins one Snapshot (table -> version) when it starts, so every query of that question,
including the independent verifier, reads the same data, and the certificate can name exactly which
data an answer came from. `LocalLakehouse` stores each table version as an immutable Parquet file
(the same idea as Iceberg / Delta snapshots); a production catalog implements the same interface over
Iceberg, Delta, Unity Catalog or Glue.
"""
from __future__ import annotations

import json
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..util import new_id, now_iso, sha, stable_json


@dataclass(frozen=True)
class Snapshot:
    tables: Tuple[Tuple[str, str], ...]          # sorted (table, version) pairs

    @property
    def id(self) -> str:
        return "snap_" + sha(stable_json(self.tables), 16)

    def version(self, table: str) -> str:
        for t, v in self.tables:
            if t == table:
                return v
        raise KeyError(f"table `{table}` is not in snapshot {self.id}")

    def as_dict(self) -> Dict[str, str]:
        return dict(self.tables)

    @classmethod
    def from_dict(cls, d: Dict[str, str]) -> "Snapshot":
        return cls(tuple(sorted(d.items())))


class Catalog(ABC):
    @abstractmethod
    def tables(self) -> List[str]: ...

    @abstractmethod
    def snapshot(self) -> Snapshot:
        """The current version of every table."""

    @abstractmethod
    def schema(self, table: str, version: str) -> List[Tuple[str, str]]:
        """[(column, SQL type)] of one table version."""

    @abstractmethod
    def row_count(self, table: str, version: str) -> Optional[int]: ...


class LocalLakehouse(Catalog):
    """<root>/<table>/_log.json lists versions; <root>/<table>/<version>.parquet holds each one, written once."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reading
    def _log(self, table: str) -> dict:
        path = os.path.join(self.root, table, "_log.json")
        return json.load(open(path)) if os.path.exists(path) else {"versions": []}

    def tables(self) -> List[str]:
        return sorted(t for t in os.listdir(self.root)
                      if os.path.exists(os.path.join(self.root, t, "_log.json")))

    def versions(self, table: str) -> List[dict]:
        return self._log(table)["versions"]

    def _entry(self, table: str, version: str) -> dict:
        for v in self.versions(table):
            if v["id"] == version:
                return v
        raise KeyError(f"table `{table}` has no version `{version}`")

    def snapshot(self) -> Snapshot:
        return Snapshot.from_dict({t: self.versions(t)[-1]["id"] for t in self.tables() if self.versions(t)})

    def schema(self, table: str, version: str) -> List[Tuple[str, str]]:
        return [tuple(c) for c in self._entry(table, version)["schema"]]

    def row_count(self, table: str, version: str) -> Optional[int]:
        return self._entry(table, version)["rows"]

    def path(self, table: str, version: str) -> str:
        return os.path.join(self.root, table, self._entry(table, version)["file"])

    def profile_path(self, table: str, version: str) -> str:
        return os.path.join(self.root, table, f"{version}.profile.json")

    # ------------------------------------------------------------------ writing (connectors use these)
    def write_from_sql(self, con, table: str, select_sql: str, source: str, note: str = "") -> str:
        """Materialise `select_sql` (run on DuckDB connection `con`) as a new immutable version of `table`."""
        with self._lock:
            os.makedirs(os.path.join(self.root, table), exist_ok=True)
            version = new_id("v")
            fname = f"{version}.parquet"
            path = os.path.join(self.root, table, fname)
            con.execute(f"COPY ({select_sql}) TO '{path}' (FORMAT PARQUET)")
            schema = [(r[0], r[1]) for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()]
            rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{path}')").fetchone()[0]
            log = self._log(table)
            log["versions"].append({"id": version, "file": fname, "rows": rows, "schema": schema,
                                    "created": now_iso(), "source": source, "note": note})
            tmp = os.path.join(self.root, table, "_log.json.tmp")
            json.dump(log, open(tmp, "w"), indent=1)
            os.replace(tmp, os.path.join(self.root, table, "_log.json"))
            return version

    def write_from_rows(self, con, table: str, columns: Sequence[Tuple[str, str]], rows: Sequence[Sequence],
                        source: str, note: str = "") -> str:
        """Write Python rows with declared SQL types, e.g. [("quarter", "DATE"), ("NETINCQ", "BIGINT")]."""
        tmp = "neo_ingest_" + sha(table + now_iso(), 8)
        cols = ", ".join(f'"{c}" {t}' for c, t in columns)
        con.execute(f"CREATE TEMP TABLE {tmp} ({cols})")
        try:
            if rows:
                marks = ", ".join("?" for _ in columns)
                con.executemany(f"INSERT INTO {tmp} VALUES ({marks})", [list(r) for r in rows])
            return self.write_from_sql(con, table, f"SELECT * FROM {tmp}", source, note)
        finally:
            con.execute(f"DROP TABLE IF EXISTS {tmp}")
