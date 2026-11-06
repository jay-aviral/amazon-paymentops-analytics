"""Database access layer shared by the ETL, the report builder and the tests.

Two targets, one set of SQL files:

* ``mysql``  – the real analytical database (MySQL 8, via PyMySQL)
* ``sqlite`` – a zero-install stand-in used for local runs and automated tests.
               The MySQL DDL is translated on the fly (the only change is dropping
               the ``ENGINE=...`` table option) and the few MySQL functions the
               SQL uses are registered as SQLite functions.

All analytical SQL therefore has to stay inside the common subset: CTEs, window
functions, CASE, standard aggregates. No vendor-specific date functions.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config

log = logging.getLogger("db")

TABLE_ORDER = ["payment_methods", "customers", "orders", "payments",
               "gateway_transactions", "refunds"]


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------
def _unix_timestamp(value):
    if value is None:
        return None
    s = str(value)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


class SQLiteConnection(sqlite3.Connection):
    """sqlite3 connection that carries its dialect name, like the MySQL one."""
    dialect = "sqlite"


def connect_sqlite(path=None, fresh: bool = False) -> sqlite3.Connection:
    path = Path(path or config.SQLITE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fresh and path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path), factory=SQLiteConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -400000")
    conn.create_function("UNIX_TIMESTAMP", 1, _unix_timestamp, deterministic=True)
    return conn


def connect_mysql(create_db: bool = True):
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise SystemExit("PyMySQL is required for --target mysql:  pip install pymysql") from exc
    cfg = config.mysql_settings()
    db_name = cfg.pop("database")
    conn = pymysql.connect(**cfg, charset="utf8mb4", autocommit=False)
    with conn.cursor() as cur:
        if create_db:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4")
        cur.execute(f"USE `{db_name}`")
        cur.execute("SET time_zone = '+00:00'")     # UNIX_TIMESTAMP() differences stay DST-proof
    conn.commit()
    conn.dialect = "mysql"             # type: ignore[attr-defined]
    return conn


def connect(target: str, fresh: bool = False, path=None):
    if target == "sqlite":
        return connect_sqlite(path=path, fresh=fresh)
    if target == "mysql":
        return connect_mysql()
    raise ValueError(f"unknown target {target!r}")


def placeholder(conn) -> str:
    return "?" if conn.dialect == "sqlite" else "%s"


# --------------------------------------------------------------------------
# SQL file helpers
# --------------------------------------------------------------------------
def _strip_comments(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


def split_statements(sql: str) -> list[str]:
    """Split a script on ';' (these scripts contain no procedures or ';' in strings)."""
    return [s.strip() for s in _strip_comments(sql).split(";") if s.strip()]


def translate_for_sqlite(stmt: str) -> str:
    return re.sub(r"\)\s*ENGINE\s*=[^;]*$", ")", stmt.strip(), flags=re.S)


def run_script(conn, path: Path, skip_indexes: bool = False, only_indexes: bool = False) -> int:
    n = 0
    cur = conn.cursor()
    for stmt in split_statements(Path(path).read_text(encoding="utf-8")):
        is_index = bool(re.match(r"CREATE\s+(UNIQUE\s+)?INDEX", stmt, re.I))
        if (skip_indexes and is_index) or (only_indexes and not is_index):
            continue
        if conn.dialect == "sqlite":
            stmt = translate_for_sqlite(stmt)
        cur.execute(stmt)
        n += 1
    conn.commit()
    return n


def create_schema(conn):
    """Tables only; indexes are added after the bulk load (much faster)."""
    run_script(conn, config.SQL_DIR / "01_schema.sql", skip_indexes=True)


def create_indexes(conn):
    run_script(conn, config.SQL_DIR / "01_schema.sql", only_indexes=True)


def create_views(conn):
    run_script(conn, config.SQL_DIR / "02_views.sql")


def load_named_queries(path: Path) -> "OrderedDict[str, dict]":
    """Parse a .sql file whose queries are introduced by ``-- @query: name``
    (optionally followed by ``-- @desc: text`` lines)."""
    queries: "OrderedDict[str, dict]" = OrderedDict()
    name, desc, buf = None, [], []

    def flush():
        if name:
            sql = "\n".join(buf).strip().rstrip(";").strip()
            queries[name] = dict(sql=sql, description=" ".join(desc).strip())

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"--\s*@query:\s*(\w+)", line)
        if m:
            flush()
            name, desc, buf = m.group(1), [], []
            continue
        m = re.match(r"--\s*@desc:\s*(.*)", line)
        if m and name:
            desc.append(m.group(1))
            continue
        if name and not line.strip().startswith("--"):
            buf.append(line)
    flush()
    return queries


def all_queries() -> "OrderedDict[str, dict]":
    out: "OrderedDict[str, dict]" = OrderedDict()
    for f in sorted(config.SQL_DIR.glob("0[3-7]_*.sql")):
        for k, v in load_named_queries(f).items():
            v["file"] = f.name
            out[k] = v
    return out


# --------------------------------------------------------------------------
# query + load
# --------------------------------------------------------------------------
def fetch_df(conn, sql: str, params=None) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cols = [d[0] for d in cur.description]
    df = pd.DataFrame(cur.fetchall(), columns=cols)
    for c in df.columns:                       # MySQL returns Decimal objects
        if df[c].dtype == object and len(df):
            try:
                df[c] = pd.to_numeric(df[c])
            except (ValueError, TypeError):
                pass
    return df


def run_query(conn, name: str) -> pd.DataFrame:
    return fetch_df(conn, all_queries()[name]["sql"])


def _to_rows(df: pd.DataFrame):
    obj = df.astype(object).where(df.notna(), None)
    return obj.itertuples(index=False, name=None)


def bulk_insert(conn, table: str, df: pd.DataFrame, batch: int = 20000) -> int:
    cols = list(df.columns)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join([placeholder(conn)] * len(cols))})"
    cur = conn.cursor()
    rows, total = [], 0
    for r in _to_rows(df):
        rows.append(r)
        if len(rows) >= batch:
            cur.executemany(sql, rows)
            total += len(rows)
            rows = []
    if rows:
        cur.executemany(sql, rows)
        total += len(rows)
    conn.commit()
    return total


def table_count(conn, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])
