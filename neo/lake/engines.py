"""Query engines and SQL dialects.

The compiler writes SQL through a Dialect, so the same model runs on DuckDB (local) or on Trino /
Snowflake / Databricks / Spark SQL through `SQLAlchemyEngine`. An engine is bound to one Snapshot:
`table_ref(table)` returns the SQL that reads exactly the pinned version (a view over an immutable
Parquet file locally; `FOR VERSION AS OF` / time travel in production).
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Iterator, List, Optional, Sequence, Tuple

from .catalog import Catalog, LocalLakehouse, Snapshot


class EngineError(RuntimeError):
    pass


class Dialect:
    """SQL spellings that differ between engines. Exact integer arithmetic uses `int_type`."""

    name = "duckdb"
    int_type = "HUGEINT"

    def q(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def lit_str(self, s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    def to_int(self, expr: str) -> str:
        return f"CAST({expr} AS {self.int_type})"

    def count_if(self, cond: str) -> str:
        return f"COUNT(*) FILTER (WHERE {cond})"

    def exact_div(self, a: str, b: str) -> str:
        """a / b when b divides a exactly (both integers)."""
        return f"(({a}) // ({b}))"

    def mod(self, a: str, b: str) -> str:
        return f"(({a}) % ({b}))"

    def contains(self, s: str, sub: str) -> str:
        return f"contains(lower({s}), lower({sub}))"

    def starts_with(self, s: str, p: str) -> str:
        return f"starts_with(lower({s}), lower({p}))"

    def decimal_type(self, scale: int) -> str:
        return f"DECIMAL(38,{scale})"

    def double_to_decimal(self, expr: str, scale: int) -> str:
        """The exact value of a floating-point column is defined as the engine's own decimal cast at a fixed scale;
        the compiler and the interpreter both read it this way, so they agree by construction."""
        return f"CAST({expr} AS {self.decimal_type(scale)})"

    def double_to_int(self, expr: str, scale: int) -> str:
        return f"CAST({self.double_to_decimal(expr, scale)} * {10 ** scale} AS {self.int_type})"

    def to_double(self, expr: str) -> str:
        return f"CAST({expr} AS DOUBLE)"

    def date_text(self, expr: str) -> str:
        return f"CAST(CAST({expr} AS DATE) AS VARCHAR)"


class TrinoDialect(Dialect):
    name = "trino"
    int_type = "DECIMAL(38,0)"

    def exact_div(self, a: str, b: str) -> str:
        return f"(({a}) / ({b}))"

    def mod(self, a: str, b: str) -> str:
        return f"mod({a}, {b})"

    def contains(self, s: str, sub: str) -> str:
        return f"(strpos(lower({s}), lower({sub})) > 0)"

    def starts_with(self, s: str, p: str) -> str:
        return f"starts_with(lower({s}), lower({p}))"


class SnowflakeDialect(TrinoDialect):
    name = "snowflake"
    int_type = "NUMBER(38,0)"

    def decimal_type(self, scale: int) -> str:
        return f"NUMBER(38,{scale})"

    def count_if(self, cond: str) -> str:
        return f"COUNT_IF({cond})"

    def contains(self, s: str, sub: str) -> str:
        return f"CONTAINS(LOWER({s}), LOWER({sub}))"


DIALECTS = {"duckdb": Dialect, "trino": TrinoDialect, "snowflake": SnowflakeDialect}


class QueryEngine(ABC):
    dialect: Dialect

    @abstractmethod
    def bind(self, snapshot: Snapshot) -> "QueryEngine":
        """An engine view that reads exactly the versions in `snapshot`."""

    @abstractmethod
    def table_ref(self, table: str) -> str: ...

    @abstractmethod
    def execute(self, sql: str) -> Tuple[List[str], List[tuple]]: ...

    def stream(self, sql: str, batch: int = 50_000) -> Iterator[Tuple[List[str], List[tuple]]]:
        cols, rows = self.execute(sql)
        for i in range(0, len(rows), batch):
            yield cols, rows[i:i + batch]


class DuckDBEngine(QueryEngine):
    """DuckDB over the LocalLakehouse. Each bound snapshot gets its own schema of read-only views."""

    def __init__(self, catalog: LocalLakehouse, snapshot: Optional[Snapshot] = None, _con=None):
        import duckdb
        self.catalog = catalog
        self.dialect = Dialect()
        self._con = _con or duckdb.connect()
        self._lock = threading.Lock()
        self.snapshot = snapshot
        self.schema_name = None
        if snapshot is not None:
            self._create_views(snapshot)

    def _create_views(self, snapshot: Snapshot):
        self.schema_name = "s_" + snapshot.id.replace("snap_", "")
        with self._lock:
            self._con.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema_name}")
            for table, version in snapshot.tables:
                path = self.catalog.path(table, version).replace("'", "''")
                self._con.execute(f'CREATE OR REPLACE VIEW {self.schema_name}."{table}" AS '
                                  f"SELECT * FROM read_parquet('{path}')")

    def bind(self, snapshot: Snapshot) -> "DuckDBEngine":
        return DuckDBEngine(self.catalog, snapshot, _con=self._con)

    @property
    def connection(self):
        return self._con

    def table_ref(self, table: str) -> str:
        if self.schema_name is None:
            raise EngineError("engine is not bound to a snapshot")
        return f'{self.schema_name}."{table}"'

    def execute(self, sql: str) -> Tuple[List[str], List[tuple]]:
        import duckdb
        try:
            cur = self._con.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, rows
        except duckdb.Error as e:
            raise EngineError(f"{type(e).__name__}: {e}")

    def stream(self, sql: str, batch: int = 50_000):
        import duckdb
        try:
            cur = self._con.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                yield cols, rows
        except duckdb.Error as e:
            raise EngineError(f"{type(e).__name__}: {e}")


class SQLAlchemyEngine(QueryEngine):
    """Any engine with a SQLAlchemy dialect (trino://, snowflake://, databricks://, ...).

    table_ref_template turns a pinned version into time travel, e.g. Trino + Iceberg:
      "{table} FOR VERSION AS OF {version}"   Snowflake: "{table} AT(STATEMENT => '{version}')"
    """

    def __init__(self, url: str, dialect: str = "trino", table_ref_template: str = "{table}",
                 snapshot: Optional[Snapshot] = None, _engine=None):
        try:
            import sqlalchemy
        except ImportError:
            raise EngineError("pip install sqlalchemy and the engine's dialect package to use lake.kind: sqlalchemy")
        self.url = url
        self.dialect = DIALECTS[dialect]()
        self.template = table_ref_template
        self.snapshot = snapshot
        self._engine = _engine or sqlalchemy.create_engine(url)

    def bind(self, snapshot: Snapshot) -> "SQLAlchemyEngine":
        return SQLAlchemyEngine(self.url, self.dialect.name, self.template, snapshot, self._engine)

    def table_ref(self, table: str) -> str:
        version = self.snapshot.version(table) if self.snapshot else ""
        return self.template.format(table=table, version=version)

    def execute(self, sql: str):
        import sqlalchemy
        try:
            with self._engine.connect() as c:
                res = c.execute(sqlalchemy.text(sql))
                return list(res.keys()), [tuple(r) for r in res.fetchall()]
        except Exception as e:  # driver-specific exception classes
            raise EngineError(f"{type(e).__name__}: {e}")


def make_engine(cfg: dict, catalog: Catalog) -> QueryEngine:
    kind = cfg["lake"].get("kind", "local")
    if kind == "local":
        return DuckDBEngine(catalog)
    if kind == "sqlalchemy":
        s = cfg["lake"]["sqlalchemy"]
        return SQLAlchemyEngine(s["url"], s.get("dialect", "trino"), s.get("table_ref", "{table}"))
    raise EngineError(f"unknown lake.kind `{kind}`")


def timed(engine: QueryEngine, sql: str):
    t0 = time.time()
    cols, rows = engine.execute(sql)
    return cols, rows, round((time.time() - t0) * 1000, 1)
