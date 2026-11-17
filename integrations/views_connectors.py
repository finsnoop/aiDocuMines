"""
Connector API used by the Integrations page.

    GET  catalog/                       every connector: configured? connected (as whom)? can import/export?
    POST <service>/auth/initiate/       -> {auth_url}            (OAuth connectors)
    GET  <service>/auth/callback/       provider redirect target -> back to /app/integrations
    GET  <service>/auth/status/         -> {is_authenticated, user_email, ...}
    POST <service>/auth/revoke/         disconnect (and ask the provider to forget the token)
    POST <service>/connect/             webhook connectors: {webhook_url} -> signing secret + inbound URL (once)
    POST <service>/test/                webhook connectors: send a signed test event
    GET  <service>/files/               ?folder_id=&page_token=  -> {files, next_page_token, has_more}
    POST <service>/import/              {file_ids, project_id, service_id}       -> background job
    POST <service>/export/              {file_ids, target_folder_id}             -> background job
    GET  jobs/<id>/                     job progress and per-file results
    POST hooks/<token>/upload/          inbound documents from Zapier / Make (multipart "file" or {"file_url"})

Nextcloud and OnlyOffice keep their own implementations (integrations/views.py) behind the same URLs.
"""
import logging
import os

from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from . import connectors as svc
from . import views as legacy
from .models import ConnectorAccount, ConnectorJob
from .providers import PROVIDERS, ConnectorError, NotSupported, get_provider

logger = logging.getLogger(__name__)
LEGACY = {"nextcloud", "onlyoffice"}
INBOUND_MAX_BYTES = int(os.getenv("CONNECTOR_INBOUND_MAX_MB", "100")) * 1024 * 1024


def _legacy(view_cls, request, service):
    return view_cls.as_view()(request._request, service=service)


def _provider_or_404(service):
    provider = get_provider(service)
    if not provider:
        return None, Response({"error": f"Unknown connector '{service}'."}, status=404)
    return provider, None


def _fail(exc: Exception, code=400):
    return Response({"error": str(exc)}, status=code)


def _is_admin(user):
    from core.access import is_platform_superuser, is_tenant_admin
    return bool(user.is_superuser or is_platform_superuser(user) or is_tenant_admin(user))


class ConnectorThrottle(UserRateThrottle):
    scope = "connectors"
    rate = os.getenv("THROTTLE_CONNECTORS", "120/min")


class CatalogView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ConnectorThrottle]

    def get(self, request):
        admin = _is_admin(request.user)
        out = []
        for p in PROVIDERS.values():
            row = svc.status_for(request.user, p)
            if admin and p.kind == "oauth":
                row["setup"] = {"env": p.setup_env(), "redirect_uri": svc.redirect_uri_for(request, p.key)}
            out.append(row)
        return Response({"connectors": out, "is_admin": admin})


class InitiateView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ConnectorThrottle]

    def post(self, request, service):
        if service in LEGACY:
            return _legacy(legacy.ConnectorAuthInitiateView, request, service)
        provider, err = _provider_or_404(service)
        if err:
            return err
        if provider.kind == "webhook":
            return _fail(ConnectorError(f"Paste your {provider.name} webhook address to connect."))
        account = svc.account_for(request.user, service)
        if account and account.access_token:
            return Response({"is_authenticated": True, "user_email": account.account_label})
        try:
            return Response({"is_authenticated": False, "auth_url": svc.start_oauth(request, provider)})
        except ConnectorError as exc:
            return _fail(exc, 409)


class CallbackView(APIView):
    """The provider sends the browser here. The single-use state identifies the user; no session needed."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, service):
        if request.query_params.get("error"):
            reason = request.query_params.get("error_description") or request.query_params.get("error")
            return HttpResponseRedirect(svc.frontend_redirect(service=service, error=str(reason)[:200]))
        try:
            acc = svc.finish_oauth(request.query_params.get("state", ""), request.query_params.get("code", ""))
            return HttpResponseRedirect(svc.frontend_redirect(connected=acc.service))
        except ConnectorError as exc:
            return HttpResponseRedirect(svc.frontend_redirect(service=service, error=str(exc)[:200]))
        except Exception:
            logger.exception("OAuth callback for %s failed", service)
            return HttpResponseRedirect(svc.frontend_redirect(service=service, error="The sign-in could not be completed."))


class StatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, service):
        if service in LEGACY:
            return _legacy(legacy.ConnectorAuthStatusView, request, service)
        provider, err = _provider_or_404(service)
        return err or Response(svc.status_for(request.user, provider))


class RevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, service):
        if service in LEGACY:
            return _legacy(legacy.ConnectorAuthRevokeView, request, service)
        provider, err = _provider_or_404(service)
        if err:
            return err
        acc = svc.account_for(request.user, service)
        if acc:
            if acc.access_token:
                try:
                    provider.revoke(svc.Api(provider, acc))
                except Exception as exc:           # the local disconnect matters more than the provider's answer
                    logger.info("Revoking %s at the provider failed: %s", service, exc)
            acc.delete()
        return Response({"success": True, "message": f"{provider.name} disconnected."})


class ConnectView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ConnectorThrottle]

    def post(self, request, service):
        if service in LEGACY:
            return _legacy(legacy.ConnectorConnectView, request, service)
        provider, err = _provider_or_404(service)
        if err:
            return err
        if provider.kind != "webhook":
            return InitiateView().post(request, service)
        try:
            return Response(svc.connect_webhook(request, provider, str(request.data.get("webhook_url", "")),
                                                request.data.get("matter_id")))
        except ConnectorError as exc:
            return _fail(exc)


class TestEventView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ConnectorThrottle]

    def post(self, request, service):
        provider, err = _provider_or_404(service)
        if err:
            return err
        acc = svc.account_for(request.user, service)
        if provider.kind != "webhook" or not acc:
            return _fail(ConnectorError(f"Connect {provider.name} with a webhook address first."))
        try:
            code = svc.deliver(acc, "test", {"message": "Hello from aiDocuMines", "user": request.user.email})
        except ConnectorError as exc:
            return _fail(exc)
        except Exception:
            return _fail(ConnectorError("The webhook address could not be reached."), 502)
        ok = 200 <= code < 300
        return Response({"delivered": ok, "status_code": code,
                         "message": "Test event delivered." if ok else f"The webhook answered {code}."},
                        status=200 if ok else 502)


class FilesView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ConnectorThrottle]

    def get(self, request, service):
        if service in LEGACY:
            return _legacy(legacy.ConnectorFilesView, request, service)
        provider, err = _provider_or_404(service)
        if err:
            return err
        try:
            api = svc.api_for(request.user, provider)
            files, nxt = provider.list(api, request.query_params.get("folder_id") or None,
                                       request.query_params.get("page_token") or None)
        except NotSupported as exc:
            return _fail(exc, 405)
        except ConnectorError as exc:
            return _fail(exc)
        return Response({"files": files, "next_page_token": nxt, "has_more": bool(nxt)})


class _JobView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ConnectorThrottle]
    kind = ""
    legacy_view = None

    def post(self, request, service):
        if service in LEGACY:
            return _legacy(self.legacy_view, request, service)
        provider, err = _provider_or_404(service)
        if err:
            return err
        if (self.kind == "import" and not provider.can_import) or (self.kind == "export" and not provider.can_export):
            return _fail(NotSupported(f"{provider.name} does not support {self.kind}."), 405)
        try:
            job = svc.create_job(request.user, provider, self.kind, dict(request.data))
        except ConnectorError as exc:
            return _fail(exc)
        return Response(svc.job_payload(job), status=status.HTTP_202_ACCEPTED)


class ImportView(_JobView):
    kind, legacy_view = "import", legacy.ConnectorImportView


class ExportView(_JobView):
    kind, legacy_view = "export", legacy.ConnectorExportView


class JobView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        job = get_object_or_404(ConnectorJob, pk=job_id, user=request.user)      # 404 for other people's jobs
        return Response(svc.job_payload(job))


class InboundThrottle(AnonRateThrottle):
    scope = "connector_inbound"
    rate = os.getenv("THROTTLE_CONNECTOR_INBOUND", "60/min")


class InboundUploadView(APIView):
    """Zapier / Make push a document in. The long random token in the URL is the credential."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [InboundThrottle]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, token):
        acc = svc.account_for_inbound(token)
        if not acc or not acc.user.is_active:
            return Response({"error": "Unknown upload address."}, status=404)
        tmp = None
        try:
            # Into a matter, like every other document: the request's matter_id, else the connection's default.
            from core.ingest import MatterBatch
            matter = svc._matter(acc.user, request.data.get("matter_id") or acc.extra.get("default_matter_id"))
            upload = request.FILES.get("file")
            if upload:
                if upload.size > INBOUND_MAX_BYTES:
                    raise ConnectorError(f"The file is larger than {INBOUND_MAX_BYTES // (1024 * 1024)} MB.")
                import tempfile
                fd, tmp = tempfile.mkstemp()
                with os.fdopen(fd, "wb") as fh:
                    for c in upload.chunks():
                        fh.write(c)
                name = upload.name
            elif request.data.get("file_url"):
                tmp, name = svc.fetch_public_file(str(request.data["file_url"]), INBOUND_MAX_BYTES)
            else:
                raise ConnectorError('Send a file (multipart field "file") or {"file_url": "https://..."}.')
            name = svc.safe_part(request.data.get("filename") or name, "document")
            batch = MatterBatch(acc.user, matter, source=acc.service)
            entry = batch.add_stream(name, _file_chunks(tmp))
            batch.finish()
        except ConnectorError as exc:
            return _fail(exc)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        result = svc.entry_result(entry)
        if result["status"] != "success":
            return Response({"error": result["error"], "filename": name}, status=400)
        return Response({"file_id": result["file_id"], "filename": name, "matter": matter.name,
                         "outcome": result["outcome"]}, status=201)


def _file_chunks(path: str, size: int = 1024 * 1024):
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(size)
            if not chunk:
                return
            yield chunk
