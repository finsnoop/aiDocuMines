"""
Guard rails for DocuChat (the Assistant page).

Everything the page lets a user point the server at goes through here:

* Model settings (`llm_config`) -- the provider must be one we run, the endpoint
  must be an allow-listed host (never an arbitrary URL that would receive document
  text), and numeric knobs are clamped.
* External databases -- only PostgreSQL / MySQL, never SQLite (local files), and
  never a loopback, link-local, private or platform-internal address unless an
  operator allow-lists it (DB_CONNECT_ALLOWED_HOSTS / DB_CONNECT_ALLOWED_NETWORKS).
  Sessions are read-only with short timeouts, tables must exist, and generated SQL
  must be a single SELECT that is row-capped.

The address check runs again immediately before every connection, and for
PostgreSQL the checked IP is pinned (hostaddr), so a DNS answer that changes
between the check and the connection cannot redirect it.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from typing import Optional
from urllib.parse import unquote, urlparse

from django.conf import settings
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

from core.llm_providers import PROVIDERS, available_chat_model, default_provider, resolve_endpoint

logger = logging.getLogger(__name__)

MAX_ROWS = 500
CONNECT_TIMEOUT = 5
STATEMENT_TIMEOUT_MS = 20_000
ALLOWED_DRIVERS = {
    "postgresql": "postgresql+psycopg2",
    "postgresql+psycopg2": "postgresql+psycopg2",
    "postgres": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mysql+pymysql": "mysql+pymysql",
}
DEFAULT_PORTS = {"postgresql+psycopg2": 5432, "mysql+pymysql": 3306}


class UnsafeTarget(ValueError):
    """The request names something the server must not connect to. The message is safe to show."""


def db_features_enabled() -> bool:
    return os.getenv("ASSISTANT_DB_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


# ── Model settings ────────────────────────────────────────────────────────────
def clean_llm_config(raw, user=None) -> dict:
    """
    Model settings for one AI call. With `user`, the organisation's AI rules apply:
    its provider and key are used, and people's own keys and endpoints are ignored
    unless the organisation allows personal keys. The organisation key is not put in
    the config (configs travel through the task queue); dispatch_to_llm reads it by
    `key_org` when it calls the model.
    """
    raw = dict(raw) if isinstance(raw, dict) else {}
    access = None
    if user is not None:
        from custom_authentication.policies import ai_access
        access = ai_access(user)
        if not access["allow_personal"]:
            raw.pop("api_key", None)
            raw.pop("endpoint", None)
        if access["provider"]:
            raw["provider"] = access["provider"]
    provider = str(raw.get("provider") or default_provider()).lower()
    if provider not in PROVIDERS:
        provider = default_provider()
    model = str(raw.get("model") or "").strip()[:200]
    if not model or not re.fullmatch(r"[\w.:/@+-]+", model):
        model = available_chat_model(provider) or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    else:
        from core.llm_providers import model_for
        model = model_for(provider, model) or model

    def num(key, default, lo, hi, cast=float):
        try:
            return max(lo, min(hi, cast(raw.get(key, default))))
        except (TypeError, ValueError):
            return default

    cfg = {
        "provider": provider,
        "model": model,
        # Only allow-listed hosts; anything else silently becomes the server default.
        "endpoint": resolve_endpoint(provider, raw.get("endpoint")),
        "temperature": num("temperature", 0.2, 0.0, 1.5),
        "timeout": num("timeout", 120, 10, 180, int),
    }
    key = str(raw.get("api_key") or "").strip()
    if key:
        cfg["api_key"] = key[:400]
    elif access and access["has_org_key"]:
        cfg["key_org"] = access["org_id"]
    elif user is None and raw.get("key_org"):
        # Internal re-clean of a config already resolved for a user (e.g. inside a task).
        cfg["key_org"] = raw["key_org"]
    if "num_ctx" in raw:
        cfg["num_ctx"] = num("num_ctx", 8192, 512, 131072, int)
    return cfg


# ── Database targets ─────────────────────────────────────────────────────────
def _env_set(name: str) -> set:
    return {v.strip().lower() for v in os.getenv(name, "").split(",") if v.strip()}


def _allowed_networks():
    nets = []
    for v in _env_set("DB_CONNECT_ALLOWED_NETWORKS"):
        try:
            nets.append(ipaddress.ip_network(v, strict=False))
        except ValueError:
            logger.warning("Ignoring bad DB_CONNECT_ALLOWED_NETWORKS entry %r", v)
    return nets


def _platform_hosts() -> set:
    """Hosts of the platform's own services: never reachable through DocuChat."""
    hosts = {"localhost"}
    for db in getattr(settings, "DATABASES", {}).values():
        if db.get("HOST"):
            hosts.add(str(db["HOST"]).lower())
    for name in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "ELASTICSEARCH_URL",
                 "ELASTICSEARCH_HOST", "MILVUS_HOST", "POSTGRES_HOST", "LITELLM_URL", "LITELLM_BASE_URL"):
        v = os.getenv(name, "")
        if v:
            h = urlparse(v).hostname if "://" in v else v.split(":")[0]
            if h:
                hosts.add(h.lower())
    return hosts


def _resolve(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeTarget(f"The database host '{host}' could not be found.")
    return sorted({ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}, key=str)


def check_host(host: str) -> str:
    """Return a vetted IP address for `host`, or raise UnsafeTarget."""
    host = (host or "").strip().lower().rstrip(".")
    if not host or len(host) > 253 or not re.fullmatch(r"[a-z0-9.:\-\[\]_]+", host):
        raise UnsafeTarget("Enter a valid database host name.")
    allowed_hosts = _env_set("DB_CONNECT_ALLOWED_HOSTS")
    if host in _platform_hosts() and host not in allowed_hosts:
        raise UnsafeTarget("That host belongs to the platform itself and cannot be queried.")
    addrs = _resolve(host.strip("[]"))
    if not addrs:
        raise UnsafeTarget(f"The database host '{host}' could not be found.")
    if host in allowed_hosts:
        return str(addrs[0])
    nets = _allowed_networks()
    for ip in addrs:
        if any(ip in n for n in nets):
            continue
        if (ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global)):
            raise UnsafeTarget("Private and internal network addresses cannot be used. "
                               "Ask your platform team to allow this database host.")
    return str(addrs[0])


def checked_url(raw) -> URL:
    """Parse and vet a connection string (or URL). Raises UnsafeTarget."""
    try:
        url = raw if isinstance(raw, URL) else make_url(unquote(str(raw or "").strip()))
    except Exception:
        raise UnsafeTarget("The connection string could not be read. Use postgresql://user:password@host:5432/database.")
    driver = ALLOWED_DRIVERS.get(url.drivername.lower())
    if not driver:
        raise UnsafeTarget("Only PostgreSQL and MySQL databases can be connected.")
    if not url.host:
        raise UnsafeTarget("The connection string must include a host.")
    if url.query:
        # Driver options (sockets, init commands, sslkeys on disk...) are not user-controllable.
        url = url.set(query={})
    port = url.port or DEFAULT_PORTS[driver]
    if not (1 <= int(port) <= 65535):
        raise UnsafeTarget("The database port is not valid.")
    check_host(url.host)
    return url.set(drivername=driver, port=port)


def url_for_request(request, data) -> URL:
    """The vetted database URL for a request's saved connection (`connection_id`, owned by the caller)."""
    from .models import DatabaseConnection

    conn_id = data.get("connection_id")
    if conn_id:
        try:
            conn = DatabaseConnection.objects.get(pk=int(conn_id), owner=request.user)
        except (DatabaseConnection.DoesNotExist, TypeError, ValueError):
            raise UnsafeTarget("That saved connection was not found.")
        return checked_url(conn.build_connection_uri())
    if data.get("connection_string"):
        raise UnsafeTarget("Raw connection strings are not accepted. Save the connection first, then choose it.")
    raise UnsafeTarget("Choose a saved connection.")


def open_engine(url):
    """A read-only, short-timeout engine for a vetted URL (re-checked here, right before use)."""
    url = checked_url(url)
    ip = check_host(url.host)
    if url.drivername.startswith("postgresql"):
        connect_args = {
            "connect_timeout": CONNECT_TIMEOUT,
            "hostaddr": ip,  # pin the address that was checked
            "options": f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
            "application_name": "aidocumines-docuchat",
        }
    else:
        connect_args = {
            "connect_timeout": CONNECT_TIMEOUT, "read_timeout": 30, "write_timeout": 30,
            "init_command": "SET SESSION TRANSACTION READ ONLY",
        }
        url = url.set(host=ip)
    return create_engine(url, poolclass=NullPool, connect_args=connect_args)


def list_tables(engine) -> list:
    insp = inspect(engine)
    names = list(insp.get_table_names())
    try:
        names += list(insp.get_view_names())
    except Exception:
        pass
    return sorted(set(names))[:2000]


def checked_table(engine, table: str) -> str:
    """The table's name if it exists in the database, else UnsafeTarget."""
    table = (table or "").strip()
    if not table or len(table) > 128:
        raise UnsafeTarget("Enter a table name.")
    if table not in list_tables(engine):
        raise UnsafeTarget(f"Table '{table}' was not found in that database.")
    return table


def quoted(engine, table: str) -> str:
    return engine.dialect.identifier_preparer.quote(table)


_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call|execute|do|vacuum|"
    r"analyze|lock|set|reset|load|handler|rename|replace|attach|detach|pragma|into)\b", re.I)


def safe_select(sql: str, max_rows: int = MAX_ROWS) -> str:
    """Accept one read-only SELECT (or WITH ... SELECT) and cap its rows; raise UnsafeTarget otherwise."""
    s = (sql or "").strip()
    s = re.sub(r"^```(?:sql)?|```$", "", s, flags=re.I).strip().rstrip(";").strip()
    if not s or ";" in s or "--" in s or "/*" in s:
        raise UnsafeTarget("The generated query was not a single statement.")
    if not re.match(r"^(select|with)\b", s, re.I) or _WRITE_WORDS.search(s):
        raise UnsafeTarget("Only read-only SELECT queries can be run.")
    return f"SELECT * FROM ({s}) AS docuchat_q LIMIT {int(max_rows)}"


def describe(url: URL) -> str:
    """host/database for messages and logs, never the password."""
    return f"{url.host}:{url.port}/{url.database or ''}"
