# db_meta_views.py

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text
from .db_query_tools import fetch_tables, fetch_column_names, scrub_db_error
from .models import DatabaseConnection


def owned_connection(request):
    connection_id = request.data.get("connection_id")
    if not connection_id:
        return None, Response({"error": "connection_id is required. Raw connection strings are not accepted."}, status=400)
    qs = DatabaseConnection.objects.filter(pk=connection_id, owner=request.user)
    client_id = getattr(request.user, "client_id", None)
    if client_id:
        qs = qs.filter(owner__client_id=client_id)
    conn = qs.first()
    if not conn:
        return None, Response({"error": "Connection not found for this account."}, status=404)
    return conn, None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_db_connection(request):
    conn, error = owned_connection(request)
    if error:
        return error
    try:
        engine = create_engine(conn.build_connection_uri())
        with engine.connect() as db:
            db.execute(text("SELECT 1"))
        engine.dispose()
        return Response({"success": True, "message": "Connection successful", "connection_id": conn.id})
    except SQLAlchemyError as e:
        return Response({"success": False, "error": scrub_db_error(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def list_tables(request):
    conn, error = owned_connection(request)
    if error:
        return error
    try:
        tables = fetch_tables(conn.build_connection_uri())
    except Exception as e:
        return Response({"error": scrub_db_error(e)}, status=400)
    return Response({"tables": tables, "connection_id": conn.id, "name": conn.name})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def list_columns(request):
    conn, error = owned_connection(request)
    if error:
        return error
    table = request.data.get("table")
    if not table:
        return Response({"error": "table is required"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        columns = fetch_column_names(conn.build_connection_uri(), table)
    except Exception as e:
        return Response({"error": scrub_db_error(e)}, status=400)
    return Response({"columns": columns, "redacted": True})

