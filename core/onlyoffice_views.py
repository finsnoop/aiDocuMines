# core/onlyoffice_views.py
import os
import time
import uuid
import jwt
import logging
import requests
import mimetypes
import traceback

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from datetime import datetime

from docx import Document as DocxDocument
from django.db import transaction
from django.contrib.auth import get_user_model


from rest_framework.parsers import JSONParser, FormParser
from urllib.parse import urlparse
from core.onlyoffice_tasks import onlyoffice_fetch_and_save


User = get_user_model()

from core.onlyoffice_tasks import onlyoffice_fetch_and_save

from core.models import File, Storage, Run
from core.onlyoffice_utils import (
    make_signed_download_url,
    verify_signed_download_token,
    ext_from_filename,
    mime_for_ext,
    ensure_unique_path,
    file_md5,
    parse_ds_response,
    redact_url,
)
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Source file ID"
)
output_type_param = openapi.Parameter(
    "output_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Target type (e.g., pdf, xlsx)"
)


class OnlyOfficeConvertView(APIView):
    """
    Convert a stored file via OnlyOffice Document Server.

    Strategy:
      1) Try /converter with BODY token (no 'payload' wrapper)
      2) If -8, try /ConvertService.ashx with HEADER token ('payload' wrapper)
      3) Try internal vs public DS bases (order controlled by ONLYOFFICE_PREFER_PUBLIC)
      4) DS requests bypass env proxies (trust_env=False)
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Convert a file to another format. Returns a new stored File record.",
        tags=["OnlyOffice"],
        manual_parameters=[file_id_param, output_type_param],
        responses={200: "Success", 202: "In Progress", 400: "Bad Request", 404: "Not Found", 502: "Upstream error"},
    )
    def post(self, request):
        dry_run = (request.query_params.get("dry_run", "0").lower() in ("1","true","yes"))
        try:
            file_id = request.query_params.get("file_id")
            output_type = (request.query_params.get("output_type") or "pdf").lower()
            if not file_id:
                return Response({"error": "Missing file_id"}, status=400)

            src = get_file_for_user(request.user, file_id)

            # Signed URL for DS to pull from our API (external host)
            source_url = make_signed_download_url(str(src.id), request=request)
            try:
                safe_source_url = redact_url(source_url)
            except Exception:
                safe_source_url = source_url.split("token=")[0] + "token=***redacted***"

            input_ext = ext_from_filename(src.filename, fallback="docx")

            # DS base selection
            prefer_public = os.getenv("ONLYOFFICE_PREFER_PUBLIC", "false").lower() == "true"
            ds_internal = settings.ONLYOFFICE["DOC_SERVER_URL"].rstrip("/")
            
            #ds_public = settings.ONLYOFFICE.get("DOC_SERVER_PUBLIC_URL", ds_internal).rstrip("/")
            #ordered = [ds_public, ds_internal] if prefer_public else [ds_internal, ds_public]
            #ds_bases = [b for b in ordered if b]
            #if "http://onlyoffice" not in ds_bases:
            #    ds_bases.append("http://onlyoffice")  # final fallback


            # DS base selection: force the public DS only (no fallbacks)
            ds_public = (
                settings.ONLYOFFICE.get("DOC_SERVER_PUBLIC_URL")
                or settings.ONLYOFFICE["DOC_SERVER_URL"]
            ).rstrip("/")
            ds_bases = [ds_public]



            secret = settings.ONLYOFFICE["JWT_SECRET"]
            conv_key = str(uuid.uuid4())
            now = int(time.time())
            exp = now + 300

            body_claims = {
                "filetype": input_ext,
                "key": conv_key,
                "outputtype": output_type,
                "title": src.filename,
                "url": source_url,
                "iat": now,
                "exp": exp,
            }
            header_claims = {
                "payload": {
                    "filetype": input_ext,
                    "key": conv_key,
                    "outputtype": output_type,
                    "title": src.filename,
                    "url": source_url,
                },
                "iat": now,
                "exp": exp,
            }

            # ---- BYPASS PROXIES ----
            sess = requests.Session()
            sess.trust_env = False

            # timeouts (connect, read)
            T_CONN = int(settings.ONLYOFFICE.get("HTTP_CONNECT_TIMEOUT", 5))
            T_READ = int(settings.ONLYOFFICE.get("HTTP_READ_TIMEOUT", 240))

            def call_converter_with_body_token(base_url: str):
                token = jwt.encode(body_claims, secret, algorithm="HS256")
                body = {
                    "async": bool(dry_run),   # fast path when dry_run=1
                    "filetype": input_ext,
                    "outputtype": output_type,
                    "key": conv_key,
                    "title": src.filename,
                    "url": source_url,
                    "token": token,
                }
                url = base_url.rstrip("/") + "/converter"
                logger.info("OnlyOffice convert (body token) POST %s", url)
                return sess.post(url, json=body, timeout=(T_CONN, T_READ))

            def call_ashx_with_header_token(base_url: str):
                token = jwt.encode(header_claims, secret, algorithm="HS256")
                headers = {"Authorization": f"Bearer {token}"}
                body = {
                    "async": bool(dry_run),   # fast path here too
                    "filetype": input_ext,
                    "outputtype": output_type,
                    "key": conv_key,
                    "title": src.filename,
                    "url": source_url,
                }
                url = base_url.rstrip("/") + "/ConvertService.ashx"
                logger.info("OnlyOffice convert (header token) POST %s", url)
                return sess.post(url, json=body, headers=headers, timeout=(T_CONN, T_READ))

            last_err_payload = None
            data = None
            used_base = None

            for base in ds_bases:
                # 1) /converter (body token)
                try:
                    r1 = call_converter_with_body_token(base)
                except requests.RequestException as e:
                    logger.warning("OnlyOffice unreachable at %s/converter: %s", base, e)
                    last_err_payload = {
                        "error": "unreachable",
                        "message": f"OnlyOffice unreachable at {base}/converter",
                        "exception": str(e),
                        "ds_base": base,
                    }
                    continue

                ok1, d1 = parse_ds_response(r1)
                if ok1:
                    data = d1
                    used_base = base
                    break

                # If DS says auth error (-8), try header-token path
                if isinstance(d1, dict) and d1.get("error") == -8:
                    try:
                        r2 = call_ashx_with_header_token(base)
                    except requests.RequestException as e:
                        logger.warning("OnlyOffice unreachable at %s/ConvertService.ashx: %s", base, e)
                        last_err_payload = {
                            "error": "unreachable",
                            "message": f"OnlyOffice unreachable at {base}/ConvertService.ashx",
                            "exception": str(e),
                            "ds_base": base,
                        }
                        continue

                    ok2, d2 = parse_ds_response(r2)
                    if ok2:
                        data = d2
                        used_base = base
                        break

                    last_err_payload = {
                        "error": d2.get("error", -1),
                        "message": "Invalid token from Document Server (header path).",
                        "ds_base": base,
                        "we_signed_fields": {
                            "filetype": input_ext,
                            "key": conv_key,
                            "outputtype": output_type,
                            "title": src.filename,
                            "url": safe_source_url,
                        },
                        "raw": d2,
                    }
                    continue

                last_err_payload = {
                    "error": d1.get("error", -1),
                    "message": "Document Server returned an error (body path).",
                    "ds_base": base,
                    "raw": d1,
                }
                continue

            if data is None:
                if not last_err_payload:
                    last_err_payload = {"error": -1, "message": "Unknown OnlyOffice error"}
                last_err_payload["hints"] = {
                    "compare_secret": "settings.ONLYOFFICE['JWT_SECRET'] must equal DS services.CoAuthoring.secret.*",
                    "note": "Tried http://onlyoffice, internal configured URL, and DOC_SERVER_PUBLIC_URL; bypassed proxies via trust_env=False.",
                }
                return Response(last_err_payload, status=502)

            # Progress?
            if not data.get("endConvert") and "percent" in data:
                return Response({"error": "Conversion in progress", "percent": data.get("percent")}, status=202)

            result_url = data.get("fileUrl")
            if not result_url:
                return Response({"error": "OnlyOffice did not return fileUrl", "raw": data}, status=502)

            # ---- dry run: prove DS works end-to-end, skip persistence
            if request.query_params.get("dry_run") == "1":
                return Response(
                    {"message": "Converted (dry run)", "fileUrl": result_url, "ds_base": used_base},
                    status=200,
                )

            # Download converted file (also bypass proxies)
            try:
                rr = sess.get(result_url, stream=True, timeout=(T_CONN, max(120, T_READ)))
                rr.raise_for_status()
            except requests.RequestException as e:
                logger.exception("Failed to fetch converted file from DS cache: %s", e)
                return Response({"error": f"Failed to fetch converted file: {e}"}, status=502)

            # Save alongside the source file (match your folder tree)
            base_dir = os.path.dirname(src.filepath)
            name_no_ext, _ = os.path.splitext(src.filename)
            out_filename = f"{name_no_ext}.{output_type}"
            out_path = ensure_unique_path(base_dir, out_filename)

            try:
                with open(out_path, "wb") as fp:
                    for chunk in rr.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fp.write(chunk)

                with transaction.atomic():
                    storage = Storage.objects.create(
                        user=src.user,
                        content_type=ContentType.objects.get_for_model(Run),
                        object_id=src.run.run_id,
                        upload_storage_location=out_path,
                    )
                    mime = mime_for_ext(output_type)
                    new_file = File.objects.create(
                        run=src.run,
                        storage=storage,
                        filename=os.path.basename(out_path),
                        filepath=out_path,
                        file_size=os.path.getsize(out_path),
                        file_type=mime,
                        md5_hash=file_md5(out_path),
                        user=src.user,
                        project_id=src.project_id,
                        service_id=src.service_id,
                        extension=output_type,
                        status=getattr(src, "status", None) or "Pending",
                    )
            except Exception as e:
                logger.exception("Persisting converted file failed")
                return Response(
                    {
                        "error": "persist_failed",
                        "message": str(e),
                        "trace": traceback.format_exc()[:4000],
                    },
                    status=500,
                )

            return Response(
                {
                    "message": "Converted successfully",
                    "source_file_id": src.id,
                    "converted_file_id": new_file.id,
                    "filename": new_file.filename,
                    "mime_type": new_file.file_type,
                    "size": new_file.file_size,
                },
                status=200,
            )
        except Exception as e:
            logger.exception("Unhandled exception in convert")
            return Response(
                {"error": "unhandled", "message": str(e), "trace": traceback.format_exc()[:4000]},
                status=500,
            )


class OnlyOfficeCommandView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Relay a coauthoring command to OnlyOffice (forcesave, etc.)",
        tags=["OnlyOffice"],
        request_body=openapi.Schema(type=openapi.TYPE_OBJECT),
        responses={200: "Success", 400: "Bad Request"},
    )
    def post(self, request):
        # base = settings.ONLYOFFICE["DOC_SERVER_URL"].rstrip("/")
        # url = base + "/coauthoring/CommandService.ashx"
        
        base = (
            settings.ONLYOFFICE.get("DOC_SERVER_PUBLIC_URL")
            or settings.ONLYOFFICE["DOC_SERVER_URL"]
        ).rstrip("/")
        url = base + "/coauthoring/CommandService.ashx"


        try:
            sess = requests.Session()
            sess.trust_env = False

            T_CONN = int(settings.ONLYOFFICE.get("HTTP_CONNECT_TIMEOUT", 5))
            T_READ = int(settings.ONLYOFFICE.get("HTTP_READ_TIMEOUT", 240))
            r = sess.post(url, json=request.data, timeout=(T_CONN, T_READ))

            ct = (r.headers.get("Content-Type") or "").lower()
            if "application/json" in ct:
                return Response(r.json(), status=r.status_code)
            return Response({"body": r.text[:1000]}, status=r.status_code)
        except requests.RequestException as e:
            return Response({"error": f"OnlyOffice unreachable: {e}"}, status=502)


@method_decorator(csrf_exempt, name="dispatch")
class OnlyOfficeSignedDownloadView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        operation_description="Internal: OnlyOffice fetches file via signed token.",
        tags=["OnlyOffice (internal)"],
        manual_parameters=[
            openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter("token", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
        ],
        responses={200: "Success", 403: "Forbidden", 404: "Not Found"},
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        token = request.query_params.get("token")
        if not (file_id and token):
            return Response({"error": "Missing file_id or token"}, status=400)

        if not verify_signed_download_token(token, str(file_id)):
            return Response({"error": "Invalid or expired token"}, status=403)

        # The Document Server calls this without a user session: the signed, expiring,
        # file-bound token IS the authorisation. Tokens are only minted (editor-config,
        # convert) for users who passed the access check on this file.
        from core.models import File
        f = File.objects.filter(pk=file_id).first()
        if f is None:
            return Response({"error": "File not found"}, status=404)

        # Prefer in-container path; fall back to stripped host-like path if needed
        real_path = f.filepath
        if not os.path.exists(real_path):
            alt = real_path.replace("/app/", "")
            if os.path.exists(alt):
                real_path = alt
            else:
                return Response({"error": "File not found"}, status=404)

        from django.http import FileResponse
        content_type, _ = mimetypes.guess_type(real_path)
        if not content_type:
            content_type = "application/octet-stream"
        return FileResponse(
            open(real_path, "rb"),
            as_attachment=False,
            filename=os.path.basename(real_path),
            content_type=content_type,
        )


def _verified_onlyoffice_payload(request):
    """
    Return the callback payload if it is signed by our Document Server, else None.
    With no JWT secret configured, signing cannot be checked: the body is returned
    and the URL-host check below remains the only guard (configure the secret!).
    """
    secret = settings.ONLYOFFICE.get("JWT_SECRET") or ""
    body = request.data or {}
    if not secret:
        logger.error("ONLYOFFICE_JWT_SECRET is not set; OnlyOffice callbacks are not signature-checked.")
        return body

    token = body.get("token") if hasattr(body, "get") else None
    from_header = False
    if not token:
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if auth.startswith("Bearer "):
            token, from_header = auth[7:], True
    if not token:
        return None
    try:
        decoded = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    # Header tokens wrap the callback body under "payload".
    if from_header and isinstance(decoded.get("payload"), dict):
        return decoded["payload"]
    return decoded


def _is_document_server_url(url: str) -> bool:
    from urllib.parse import urlparse

    allowed = set()
    for key in ("DOC_SERVER_URL", "DOC_SERVER_PUBLIC_URL"):
        host = urlparse(settings.ONLYOFFICE.get(key) or "").hostname
        if host:
            allowed.add(host.lower())
    extra = os.getenv("ONLYOFFICE_CALLBACK_ALLOWED_HOSTS", "")
    allowed |= {h.strip().lower() for h in extra.split(",") if h.strip()}
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() in allowed


@method_decorator(csrf_exempt, name="dispatch")
class OnlyOfficeCallbackView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [JSONParser, FormParser]

    @swagger_auto_schema(
        operation_description="Callback for ONLYOFFICE MustSave/ForceSave events. Must return {'error': 0}.",
        tags=["OnlyOffice"],
        request_body=openapi.Schema(type=openapi.TYPE_OBJECT),
        responses={200: "OK"},
    )
    def post(self, request):
        """
        ALWAYS return {"error": 0} so the editor considers the save successful.
        We enqueue our own async fetch regardless of internal errors.
        """
        try:
            file_id = request.query_params.get("file_id")
            if not file_id:
                logger.warning("OO callback missing file_id; payload=%s", request.data)
                return Response({"error": 0}, status=200)

            # SECURITY: this endpoint is public (the Document Server calls it), so
            # the request must prove it came from OUR Document Server:
            #   1. JWT signed with the shared secret (body "token" or Authorization header);
            #      the SIGNED payload is used, never the unsigned body.
            #   2. The download URL must point at the configured Document Server host.
            payload = _verified_onlyoffice_payload(request)
            if payload is None:
                logger.warning("OO callback rejected: missing/invalid JWT for file_id=%s", file_id)
                return Response({"error": 1, "message": "invalid token"}, status=403)
            # normalize status
            try:
                status = int(str(payload.get("status", "0")).strip() or "0")
            except Exception:
                status = 0

            # OnlyOffice sometimes sends 'changesurl' instead of 'url'
            url = (payload.get("url") or payload.get("changesurl") or "").strip()

            if url and not _is_document_server_url(url):
                logger.warning("OO callback rejected: url host not the Document Server (file_id=%s)", file_id)
                return Response({"error": 1, "message": "untrusted url"}, status=403)

            # Queue async download if we got a MustSave(2) or ForceSave(6) with a URL
            if status in (2, 6) and url:
                try:
                    onlyoffice_fetch_and_save.delay(int(file_id), url)
                except Exception as e:
                    logger.exception("Failed to enqueue onlyoffice_fetch_and_save: %s", e)
                    # still ACK with error:0

            # ✅ The ONLY response shape DS accepts as 'OK'
            return Response({"error": 0}, status=200)

        except Exception as e:
            logger.exception("OnlyOffice callback unexpected error: %s", e)
            # Still ACK so the editor doesn't show an error or force download
            return Response({"error": 0}, status=200)


'''
@method_decorator(csrf_exempt, name="dispatch")
class OnlyOfficeCallbackView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [JSONParser, FormParser]  # accept JSON (and simple form just in case)

    @swagger_auto_schema(
        operation_description="Callback endpoint for OnlyOffice editors (MustSave/ForceSave).",
        tags=["OnlyOffice"],
        request_body=openapi.Schema(type=openapi.TYPE_OBJECT),
        responses={200: "OK"},
    )
    def post(self, request):
        """
        Handle OnlyOffice callback. We *never* block here:
          - If status ∈ {2 (MustSave), 6 (ForceSave)} and a valid URL is provided,
            queue the save task and return 200.
          - Otherwise, ACK with 200 so the editor doesn’t retry loudly.
        """
        try:
            file_id = request.query_params.get("file_id")
            if not file_id:
                return Response({"ok": False, "error": "missing_file_id"}, status=400)

            payload = request.data or {}
            # status can be str or int; normalize defensively
            try:
                status = int(str(payload.get("status", "0")).strip() or "0")
            except Exception:
                status = 0

            # some DS variants send "changesurl"; prefer "url"
            url = (payload.get("url") or payload.get("changesurl") or "").strip()

            def _valid_http(u: str) -> bool:
                p = urlparse(u)
                return p.scheme in ("http", "https") and bool(p.netloc)

            # If not a save status or URL is missing/invalid -> just ACK (no download)
            if status not in (2, 6) or not _valid_http(url):
                return Response({"ok": True, "ignored": True, "status": status, "has_url": bool(url)}, status=200)

            # Queue async download+atomic write; return immediately
            try:
                onlyoffice_fetch_and_save.delay(int(file_id), url)
                return Response({"ok": True, "queued": True, "status": status}, status=200)
            except Exception as e:
                logger.exception("Failed to enqueue onlyoffice_fetch_and_save")
                # Still ACK so DS doesn't go into retry storm
                return Response({"ok": True, "queued": False, "error": str(e)}, status=200)

        except Exception as e:
            logger.exception("OnlyOffice callback error")
            # Return 200 even on unexpected errors to keep the editor happy
            return Response({"ok": True, "error": "callback_error", "message": str(e)}, status=200)
'''


class OnlyOfficeEditorConfigView(APIView):
    """
    Returns the config object and the public DocServer JS URL so the frontend
    can do: new DocsAPI.DocEditor('container', config).
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get OnlyOffice editor config for a file.",
        tags=["OnlyOffice"],
        manual_parameters=[
            openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
        ],
        responses={200: "Success", 400: "Bad Request", 404: "Not Found"},
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)

        f = get_file_for_user(request.user, file_id)

        # File URL OnlyOffice server will fetch (server-to-server)
        download_url = make_signed_download_url(str(f.id), request=request)

        # Public DS URL for the browser to load api.js
        ds_public = (
            settings.ONLYOFFICE.get("DOC_SERVER_PUBLIC_URL")
            or settings.ONLYOFFICE["DOC_SERVER_URL"]
        ).rstrip("/")

        doc_api_js = ds_public + "/web-apps/apps/api/documents/api.js"

        ext = ext_from_filename(f.filename, fallback="docx")
        # Map extension to OnlyOffice document type
        _ext_doctype_map = {"pptx": "slide", "ppt": "slide", "xlsx": "spreadsheet", "xls": "spreadsheet"}
        document_type = _ext_doctype_map.get(ext, "word")

        # Inject current user; fall back to file owner
        u = getattr(request, "user", None) or f.user
        user_id = str(getattr(u, "id", "user"))
        user_name = getattr(u, "username", None) or getattr(u, "email", None) or "User"

        # Optional callback used by editors for save events
        callback_url = settings.API_BASE_URL.rstrip("/") + f"/api/v1/core/onlyoffice/callback/?file_id={f.id}"

        cfg = {
            "documentType": document_type,
            "document": {
                "fileType": ext,
                "title": f.filename,
                "key": f"file-{f.id}-{int(time.time())}",
                "url": download_url,
                "permissions": {
                    "edit": True,
                    "download": True,
                    "print": True,
                    "comment": True,
                    "fillForms": True,
                    "review": True,
                },
            },
            "editorConfig": {
                "callbackUrl": callback_url,
                "lang": "en",
                "mode": "edit",
                "user": {"id": user_id, "name": user_name},
            },
        }

        # If JWT is enabled on DS, include signed token of the config
        # secret = settings.ONLYOFFICE["JWT_SECRET"]
        # token = jwt.encode(
        #     {"payload": cfg, "iat": int(time.time()), "exp": int(time.time()) + 300},
        #     secret,
        #     algorithm="HS256",
        # )
        # cfg["token"] = token


        # If JWT is enabled on DS, sign the CONFIG ITSELF for the web editor
        secret = settings.ONLYOFFICE["JWT_SECRET"]
        now = int(time.time())
        cfg_token_payload = {**cfg, "iat": now, "exp": now + 300}  # top-level fields mirror config
        token = jwt.encode(cfg_token_payload, secret, algorithm="HS256")
        cfg["token"] = token


        return Response({"docServerApiJs": doc_api_js, "config": cfg}, status=200)


class OnlyOfficeCreateDocxView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Create a blank DOCX and register it like uploads do.",
        tags=["OnlyOffice"],
        manual_parameters=[
            openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter("service_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter("title", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={201: "Created", 400: "Bad Request"},
    )

    def post(self, request):
        user = get_object_or_404(User, id=1) if not getattr(request, "user", None) else request.user
        project_id = (request.query_params.get("project_id") or "").strip()
        service_id = (request.query_params.get("service_id") or "").strip()
        title_in   = (request.query_params.get("title") or "Untitled.docx").strip()
        if not (project_id and service_id):
            return Response({"error": "Missing project_id or service_id"}, status=400)

        client_id = request.headers.get("X-Client-ID", "client")

        # Ensure .docx on the DB-visible name as well
        title = title_in if title_in.lower().endswith(".docx") else f"{title_in}.docx"

        # Use UTC like your upload view
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        day_segment  = ts[:8]   # YYYYMMDD
        time_segment = ts[8:]   # HHMMSS

        media_root = getattr(settings, "MEDIA_ROOT", "") or "/app/media"
        base_dir = os.path.join(
            media_root, "uploads", client_id, str(user.id), project_id, service_id, day_segment
        )
        os.makedirs(base_dir, exist_ok=True)

        # Filepath carries the HHMMSS_ prefix (same as uploads)
        out_name = f"{time_segment}_{title}"
        out_path = os.path.join(base_dir, out_name)

        # Create a blank docx with one empty paragraph
        doc = DocxDocument()
        doc.add_paragraph(" ")
        doc.save(out_path)

        md5 = file_md5(out_path)

        with transaction.atomic():
            run = Run.objects.create(user=user, status="Pending")  # uploads use Pending
            storage = Storage.objects.create(
                user=user,
                content_type=ContentType.objects.get_for_model(Run),
                object_id=run.run_id,
                upload_storage_location=out_path,
            )

            # IMPORTANT: keep original title as DB filename (no HHMMSS_ prefix)
            f = File.objects.create(
                run=run,
                storage=storage,
                filename=title,                              # <-- changed
                filepath=out_path,                           # timestamped path
                file_size=os.path.getsize(out_path),
                file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                md5_hash=md5,
                user=user,
                project_id=project_id,
                service_id=service_id,
                extension="docx",
                status="Pending",
            )

        # Link into your folder tree (same semantics as uploads)
        try:
            from document_operations.utils import get_or_create_folder_tree, link_file_to_folder
            # out_path looks like .../<project>/<service>/<YYYYMMDD>/<HHMMSS>_<title>
            relative_path = f.filepath.split(f"{project_id}/{service_id}/", 1)[-1]
            folder_parts = os.path.dirname(relative_path).split("/")
            leaf_folder = get_or_create_folder_tree(
                folder_parts,
                user=user,
                project_id=project_id,
                service_id=service_id,
            )
            link_file_to_folder(f, leaf_folder)
        except Exception as e:
            logger.warning("Folder link failed for file %s: %s", f.id, e)

        # (Optional) mirror any post-upload tasks you want here:
        # transaction.on_commit(lambda: index_file.delay(f.id))  # etc.

        return Response({"file_id": f.id, "filename": f.filename}, status=201)


class OnlyOfficeDocServerUrlView(APIView):
    """
    Returns the public Document Server base URL and api.js URL.
    Optional: ?check=1 to HEAD api.js and report basic health.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get OnlyOffice Document Server base and api.js URLs. Add ?check=1 to run a quick health check.",
        tags=["OnlyOffice"],
        manual_parameters=[
            openapi.Parameter("check", openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: "OK"},
    )
    def get(self, request):
        ds_public = (
            settings.ONLYOFFICE.get("DOC_SERVER_PUBLIC_URL")
            or settings.ONLYOFFICE["DOC_SERVER_URL"]
        ).rstrip("/")

        api_js = ds_public + "/web-apps/apps/api/documents/api.js"
        payload = {
            "docServerUrl": ds_public + "/",   # trailing slash for Angular helper
            "docServerApiJs": api_js,
        }

        if (request.query_params.get("check", "0").lower() in ("1", "true", "yes")):
            import time, requests
            t0 = time.time()
            ok = False
            status = None
            error = None
            sess = requests.Session()
            sess.trust_env = False
            try:
                r = sess.head(api_js, timeout=(
                    int(settings.ONLYOFFICE.get("HTTP_CONNECT_TIMEOUT", 5)),
                    int(settings.ONLYOFFICE.get("HTTP_READ_TIMEOUT", 10)),
                ))
                status = r.status_code
                ok = (200 <= status < 300)
            except Exception as e:
                error = str(e)
            payload["health"] = {
                "ok": ok,
                "status": status,
                "ms": int((time.time() - t0) * 1000),
                "checked": api_js,
                "error": error,
            }

        return Response(payload, status=200)

