# db_meta_views.py
"""
DocuChat database helpers: test a connection, list tables, list columns.

Body: {"connection_id": <saved connection>}, plus "table" for columns. Raw connection
strings are not accepted: a connection is saved (and its host vetted) first. Targets are vetted by safety.py (no SQLite, no internal or
platform hosts, read-only sessions, short timeouts). Errors never echo driver output,
which can contain credentials or internal addresses.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from custom_authentication.permissions import IsClientOrAdminOrSuperUser
from custom_authentication.policies import assistant_policy

from . import safety
from .db_query_tools import fetch_column_names, fetch_tables, test_connection

logger = logging.getLogger(__name__)


class DbToolsThrottle(UserRateThrottle):
    scope = "docuchat-db"
    rate = "30/min"


def _guard(request):
    if not safety.db_features_enabled():
        return Response({"error": "Database questions are turned off on this platform."}, status=403)
    if not assistant_policy(request.user)["enabled"]:
        return Response({"error": "The Assistant is turned off for your organisation."}, status=403)
    return None


def _failed(exc, what):
    if isinstance(exc, safety.UnsafeTarget):
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    logger.info("DocuChat %s failed: %s", what, type(exc).__name__)
    return Response({"error": "The database could not be reached with those details. "
                              "Check the host, port, database name, user name and password."},
                    status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsClientOrAdminOrSuperUser])
@throttle_classes([DbToolsThrottle])
def test_db_connection(request):
    blocked = _guard(request)
    if blocked:
        return blocked
    try:
        url = safety.url_for_request(request, request.data)
        test_connection(url)
        return Response({"success": True, "message": f"Connected to {safety.describe(url)}."})
    except Exception as exc:
        return _failed(exc, "connection test")


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsClientOrAdminOrSuperUser])
@throttle_classes([DbToolsThrottle])
def list_tables(request):
    blocked = _guard(request)
    if blocked:
        return blocked
    try:
        url = safety.url_for_request(request, request.data)
        return Response({"tables": fetch_tables(url), "database": safety.describe(url),
                         "connection_id": request.data.get("connection_id")})
    except Exception as exc:
        return _failed(exc, "table list")


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsClientOrAdminOrSuperUser])
@throttle_classes([DbToolsThrottle])
def list_columns(request):
    blocked = _guard(request)
    if blocked:
        return blocked
    if not request.data.get("table"):
        return Response({"error": "table is required"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        url = safety.url_for_request(request, request.data)
        return Response({"columns": fetch_column_names(url, request.data["table"]), "redacted": True})
    except Exception as exc:
        return _failed(exc, "column list")
