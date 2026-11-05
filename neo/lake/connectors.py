"""Connectors bring outside data into the lake as new immutable table versions.

Everything a model can read lives in the lake and is described by the ontology, so external sources
(the deals CSV, other exports) are ingested here rather than called during a question. That keeps
every answer reproducible from a snapshot.
"""
from __future__ import annotations

import os
from typing import Sequence, Tuple

from .catalog import LocalLakehouse


def ingest_csv(lake: LocalLakehouse, con, table: str, path: str, note: str = "") -> str:
    p = os.path.abspath(path).replace("'", "''")
    return lake.write_from_sql(con, table, f"SELECT * FROM read_csv_auto('{p}', header=true)", f"csv:{path}", note)


def ingest_rows(lake: LocalLakehouse, con, table: str, columns: Sequence[Tuple[str, str]], rows, source: str,
                note: str = "") -> str:
    return lake.write_from_rows(con, table, columns, rows, source, note)
