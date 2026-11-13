# db_query_tools.py
"""
Read-only access to a user's own external database for DocuChat.

Every function takes a connection URL, vets it (safety.checked_url) and connects
read-only with short timeouts (safety.open_engine). Columns that look like secrets
(passwords, tokens, keys, hashes, SSNs...) are never listed, selected, sent to the
model or returned. Passwords are never logged.
"""
import logging
import re

import pandas as pd
import sqlalchemy
from sqlalchemy import inspect

from . import safety

logger = logging.getLogger(__name__)

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|ssn|private[_-]?key|totp|client_secret|"
    r"refresh_token|access_token|hash)",
    re.I,
)


def assert_identifier(name: str) -> str:
    if not name or not IDENT.match(str(name)):
        raise safety.UnsafeTarget("Invalid table or column name.")
    return str(name)


def is_sensitive_column(name: str) -> bool:
    return bool(SENSITIVE.search(name or ""))


def public_columns(columns):
    return [c for c in columns if c and not is_sensitive_column(c)]


def scrub_db_error(exc) -> str:
    msg = str(exc)
    msg = re.sub(r"://[^@\s/]+@", "://***@", msg)
    msg = re.sub(r"(password|passwd|secret|token)=([^\s&]+)", r"\1=***", msg, flags=re.I)
    return msg[:300]


def fetch_tables(connection_string):
    """Tables and views in the database (plain identifiers only)."""
    engine = safety.open_engine(connection_string)
    try:
        return [t for t in safety.list_tables(engine) if IDENT.match(t)]
    finally:
        engine.dispose()


def fetch_column_names(connection_string, table):
    """Non-sensitive column names of an existing table."""
    engine = safety.open_engine(connection_string)
    try:
        table = safety.checked_table(engine, assert_identifier(table))
        return public_columns([c["name"] for c in inspect(engine).get_columns(table)])
    finally:
        engine.dispose()


def generate_sql_query(table, user_query, column_names, llm_config=None):
    """
    Ask the configured model for one SELECT over `table` using only the listed (non-sensitive)
    columns. Returns None when no usable query comes back; callers then fall back to a plain
    capped SELECT of the public columns.
    """
    from .utils import dispatch_to_llm

    schema = f"Table: {table}\nColumns: {', '.join(column_names)}"
    instruction = (
        "Write one read-only SQL SELECT statement over the table above that answers the request below. "
        "Use only the listed columns and never SELECT *. Return only the SQL, with no explanation and no code fences.\n\n"
        f"Request: {user_query}"
    )
    try:
        cfg = safety.clean_llm_config(llm_config or {})
        sql = dispatch_to_llm(instruction, schema, cfg, previous_messages=None)
        sql = safety.safe_select(sql)
        if SENSITIVE.search(sql):
            raise safety.UnsafeTarget("Sensitive columns cannot be queried.")
        return sql
    except safety.UnsafeTarget as exc:
        logger.info("Generated SQL rejected: %s", exc)
    except Exception as exc:
        logger.warning("SQL generation failed: %s", type(exc).__name__)
    return None


def execute_sql_query(connection_string, sql_query, stream=False, chunk_size=100):
    """Run a row-capped SELECT in a read-only session (always re-vetted here); sensitive columns are dropped."""
    sql_query = safety.safe_select(sql_query)
    engine = safety.open_engine(connection_string)
    try:
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True).execute(sqlalchemy.text(sql_query))
            cols = list(result.keys())
            df = pd.DataFrame(result.fetchmany(safety.MAX_ROWS), columns=cols)
            hidden = [c for c in df.columns if is_sensitive_column(str(c))]
            return df.drop(columns=hidden, errors="ignore") if hidden else df
    finally:
        engine.dispose()


def preview_table(connection_string, table, limit=5):
    """A few sample rows of an existing table, without sensitive columns."""
    engine = safety.open_engine(connection_string)
    try:
        table = safety.checked_table(engine, assert_identifier(table))
        cols = public_columns([c["name"] for c in inspect(engine).get_columns(table)])
        if not cols:
            return {"columns": [], "sample_data": [], "redacted": True}
        q = engine.dialect.identifier_preparer.quote
        sql = f"SELECT {', '.join(q(c) for c in cols)} FROM {q(table)} LIMIT {max(1, min(int(limit), 25))}"
        with engine.connect() as connection:
            rows = connection.execute(sqlalchemy.text(sql)).mappings().all()
        return {"columns": cols, "sample_data": [{k: str(v) if v is not None else None for k, v in r.items()} for r in rows],
                "redacted": True}
    finally:
        engine.dispose()


def overview_query(connection_string, table, limit=5):
    """(columns, DataFrame) sample of an existing table, without sensitive columns."""
    p = preview_table(connection_string, table, limit)
    return p["columns"], pd.DataFrame(p["sample_data"], columns=p["columns"])


def fallback_select(connection_string, table):
    """SELECT of the table's public columns (never *), capped."""
    engine = safety.open_engine(connection_string)
    try:
        table = safety.checked_table(engine, assert_identifier(table))
        cols = public_columns([c["name"] for c in inspect(engine).get_columns(table)])
        if not cols:
            raise safety.UnsafeTarget("No non-sensitive columns are available to query on this table.")
        q = engine.dialect.identifier_preparer.quote
        return f"SELECT {', '.join(q(c) for c in cols)} FROM {q(table)} LIMIT 100"
    finally:
        engine.dispose()


def test_connection(connection_string):
    """Verify whether the DB is reachable and credentials are valid (raises on failure)."""
    engine = safety.open_engine(connection_string)
    try:
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
    finally:
        engine.dispose()
