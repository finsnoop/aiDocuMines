# db_query_tools.py
"""
Read-only access to a user's own external database for DocuChat.

Every function takes a connection string or URL, vets it (safety.checked_url) and
connects read-only with short timeouts (safety.open_engine). Passwords are never
logged.
"""
import logging

import pandas as pd
import sqlalchemy
from sqlalchemy import inspect

from . import safety

logger = logging.getLogger(__name__)


def fetch_tables(connection_string):
    """Tables and views in the database."""
    engine = safety.open_engine(connection_string)
    try:
        return safety.list_tables(engine)
    finally:
        engine.dispose()


def fetch_column_names(connection_string, table):
    """Column names of an existing table."""
    engine = safety.open_engine(connection_string)
    try:
        table = safety.checked_table(engine, table)
        return [c["name"] for c in inspect(engine).get_columns(table)]
    finally:
        engine.dispose()


def generate_sql_query(table, user_query, column_names, llm_config=None):
    """
    Ask the configured model for one SELECT over `table`. Returns None when no usable
    query comes back; callers then fall back to a plain capped SELECT.
    """
    from .utils import dispatch_to_llm

    schema = f"Table: {table}\nColumns: {', '.join(column_names)}"
    instruction = (
        "Write one read-only SQL SELECT statement over the table above that answers the request below. "
        "Use only the listed columns. Return only the SQL, with no explanation and no code fences.\n\n"
        f"Request: {user_query}"
    )
    try:
        cfg = safety.clean_llm_config(llm_config or {})
        sql = dispatch_to_llm(instruction, schema, cfg, previous_messages=None)
        return safety.safe_select(sql)
    except safety.UnsafeTarget as exc:
        logger.info("Generated SQL rejected: %s", exc)
    except Exception as exc:
        logger.warning("SQL generation failed: %s", exc)
    return None


def execute_sql_query(connection_string, sql_query, stream=False, chunk_size=100):
    """Run a row-capped SELECT in a read-only session and return a DataFrame (always re-vetted here)."""
    sql_query = safety.safe_select(sql_query)
    engine = safety.open_engine(connection_string)
    try:
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True).execute(sqlalchemy.text(sql_query))
            cols = list(result.keys())
            rows = result.fetchmany(safety.MAX_ROWS)
            return pd.DataFrame(rows, columns=cols)
    finally:
        engine.dispose()


def overview_query(connection_string, table, limit=5):
    """A few sample rows of an existing table."""
    engine = safety.open_engine(connection_string)
    try:
        table = safety.checked_table(engine, table)
        sql = f"SELECT * FROM {safety.quoted(engine, table)} LIMIT {int(limit)}"
        with engine.connect() as connection:
            result = connection.execute(sqlalchemy.text(sql))
            return [c["name"] for c in inspect(engine).get_columns(table)], pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    finally:
        engine.dispose()


def fallback_select(connection_string, table):
    engine = safety.open_engine(connection_string)
    try:
        table = safety.checked_table(engine, table)
        return f"SELECT * FROM (SELECT * FROM {safety.quoted(engine, table)}) AS docuchat_q LIMIT 100"
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
