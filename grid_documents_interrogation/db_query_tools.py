# db_query_tools.py

import re
import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, inspect, select, table as sql_table, column
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from urllib.parse import unquote
from sqlalchemy.engine.url import make_url

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|ssn|private[_-]?key|totp|client_secret|refresh_token|access_token|hash)",
    re.I,
)


def assert_identifier(name: str) -> str:
    if not name or not IDENT.match(str(name)):
        raise ValueError("Invalid table or column name")
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


# Initialize LLaMA model from Ollama
llama = ChatOllama(model="llama3.1:latest", temperature=0.5)


def safe_create_engine(connection_string):
    try:
        parsed_url = make_url(unquote(connection_string))
        safe_url = parsed_url.set(password="***")
        print(f"[🔌 Creating engine for]: {safe_url}")
        return create_engine(parsed_url)
    except Exception as e:
        print(f"[❌ ERROR in safe_create_engine]: {e}")
        raise


'''
def safe_create_engine(connection_string):
    try:
        # Unquote password and parse properly
        parsed_url = make_url(unquote(connection_string))
        return create_engine(parsed_url)
    except Exception as e:
        print(f"[❌ ERROR in safe_create_engine]: {e}")
        raise
'''

def fetch_tables(connection_string):
    """List user tables. Does not swallow errors."""
    engine = safe_create_engine(connection_string)
    try:
        inspector = inspect(engine)
        return [name for name in inspector.get_table_names() if IDENT.match(name)]
    finally:
        engine.dispose()


def fetch_column_names(connection_string, table):
    """Fetch column names. Sensitive columns are omitted."""
    table = assert_identifier(table)
    engine = safe_create_engine(connection_string)
    try:
        inspector = inspect(engine)
        columns = inspector.get_columns(table)
        return public_columns([col["name"] for col in columns])
    finally:
        engine.dispose()


def preview_table(connection_string, table, limit=20):
    """Read a small sample without sensitive columns or raw SQL."""
    table = assert_identifier(table)
    cols = fetch_column_names(connection_string, table)
    if not cols:
        return {"columns": [], "sample_data": [], "redacted": True}
    engine = safe_create_engine(connection_string)
    try:
        tbl = sql_table(table, *[column(assert_identifier(c)) for c in cols])
        stmt = select(tbl).limit(max(1, min(int(limit), 25)))
        with engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {
            "columns": cols,
            "sample_data": [dict(row) for row in rows],
            "redacted": True,
        }
    finally:
        engine.dispose()


def guard_select(sql: str, table: str) -> str:
    cleaned = (sql or "").strip()
    cleaned = re.sub(r"^```sql\s*|\s*```$", "", cleaned, flags=re.I).strip().rstrip(";")
    if not cleaned or ";" in cleaned or "--" in cleaned or "/*" in cleaned:
        raise ValueError("Only a single SELECT statement is allowed")
    if not re.match(r"(?is)^select\s", cleaned):
        raise ValueError("Only SELECT queries are allowed")
    if re.search(r"(?i)\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|into|outfile|pg_sleep|execute)\b", cleaned):
        raise ValueError("Write or admin SQL is not allowed")
    if "*" in cleaned.split("from", 1)[0]:
        raise ValueError("SELECT * is not allowed")
    if SENSITIVE.search(cleaned):
        raise ValueError("Sensitive columns cannot be queried")
    if not re.search(rf"(?i)\b{re.escape(assert_identifier(table))}\b", cleaned):
        raise ValueError("Query must use only the selected table")
    if not re.search(r"(?i)\blimit\b", cleaned):
        cleaned += " LIMIT 25"
    return cleaned


def generate_sql_query(table, user_query, column_names):
    columns_str = ", ".join(column_names)
    prompt = f"""
    Convert the following natural language request into a SQL query for the table `{table}`.
    Available columns: {columns_str}

    Request: {user_query}
    Only return the SQL query.
    """
    try:
        template = ChatPromptTemplate.from_template(prompt)
        parser = StrOutputParser()
        chain = template | llama | parser
        sql_query = chain.invoke({"question": user_query})

        print(f"[🧠 SQL GENERATED]: {sql_query.strip()}")
        return sql_query.strip().rstrip(";")
    except Exception as e:
        print(f"[❌ SQL GENERATION FAILED]: {e}")
        return f"SELECT * FROM {table} LIMIT 100"


def execute_sql_query(connection_string, sql_query, stream=False, chunk_size=100):
    """
    Run the SQL query and return results either all-at-once or in chunks.
    If streaming, loads all chunks into memory and returns a single DataFrame.
    """
    try:
        engine = safe_create_engine(connection_string)
        with engine.connect() as connection:
            if stream:
                result = connection.execution_options(stream_results=True).execute(sqlalchemy.text(sql_query))
                df_chunks = []
                while True:
                    rows = result.fetchmany(chunk_size)
                    if not rows:
                        break
                    df_chunks.append(pd.DataFrame(rows, columns=result.keys()))
                return pd.concat(df_chunks, ignore_index=True) if df_chunks else pd.DataFrame()
            else:
                return pd.read_sql_query(sql_query, con=connection)
    except Exception as e:
        print(f"[Error] SQL execution failed: {e}")
        return pd.DataFrame([{"error": str(e)}])



'''
def execute_sql_query(connection_string, sql_query, stream=False, chunk_size=100):
    """Run the SQL query and return results either all-at-once or in chunks."""
    try:
        engine = safe_create_engine(connection_string) # create_engine(connection_string)
        with engine.connect() as connection:
            if stream:
                result = connection.execution_options(stream_results=True).execute(sqlalchemy.text(sql_query))
                while True:
                    rows = result.fetchmany(chunk_size)
                    if not rows:
                        break
                    yield pd.DataFrame(rows, columns=result.keys())
            else:
                return pd.read_sql_query(sql_query, con=connection)
    except Exception as e:
        print(f"[Error] SQL execution failed: {e}")
        return pd.DataFrame([{"error": str(e)}])
'''


def test_connection(connection_string):
    """Verify whether the DB is reachable and credentials are valid."""
    try:
        engine = safe_create_engine(connection_string)
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        print("[✅ DB Connection Successful]")
    except Exception as e:
        print(f"[❌ DB Connection Failed]: {e}")
        raise

