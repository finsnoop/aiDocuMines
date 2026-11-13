# integrations/views.py

import os
import secrets
import time
import hashlib
import mimetypes
from django.views import View
from django.shortcuts import redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponseServerError

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny
from rest_framework import status, generics, filters

from django.contrib.auth import get_user_model

from integrations.oidc_utils import get_or_create_nextcloud_oidc_user, generate_nextcloud_oidc_url
from integrations.tasks import generate_nextcloud_url_async, sync_user_to_nextcloud_host
import re
from integrations.models import IntegrationLog, NextcloudConnection, ConnectorEndpointRequest
from integrations.nextcloud_webdav import list_files as nc_list_files
from integrations.nextcloud_webdav import download_file as nc_download_file
from integrations.nextcloud_webdav import upload_file as nc_upload_file
from integrations.nextcloud_webdav import ensure_folder as nc_ensure_folder
from integrations.nextcloud_webdav import test_connection as nc_test_connection
from .serializers import IntegrationLogSerializer

from integrations.registry import STATE_REGISTRY, NONCE_REGISTRY


class ConnectorEndpointRequestView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ConnectorEndpointRequest.objects.filter(user=request.user)
        client_id = getattr(request.user, "client_id", None)
        if client_id:
            qs = qs.filter(user__client_id=client_id)
        return Response([
            {"id": row.id, "source": row.source, "note": row.note, "created_at": row.created_at}
            for row in qs[:50]
        ])

    def post(self, request):
        source = (request.data.get("source") or "").strip()[:80]
        note = (request.data.get("note") or "").strip()[:500]
        if not source:
            return Response({"error": "source is required"}, status=400)
        if re.search(r"(password|secret|token|api[_-]?key)", note, re.I):
            return Response({"error": "Do not include passwords or tokens in a request."}, status=400)
        row, created = ConnectorEndpointRequest.objects.update_or_create(
            user=request.user,
            source=source,
            defaults={"note": note},
        )
        return Response({"id": row.id, "source": row.source, "created": created}, status=201)


class ConnectorAuthInitiateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, service):
        user = request.user

        if service == "nextcloud":
            try:
                get_or_create_nextcloud_oidc_user(user)

                url = generate_nextcloud_oidc_url(user)

                IntegrationLog.objects.create(
                    user=user,
                    connector="nextcloud",
                    status="processing",
                    details=f"OIDC login URL generated for user {user.id}"
                )

                return Response({
                    "auth_url": url,
                    "is_authenticated": False,
                    "user_email": user.email,
                })

            except Exception as e:
                generate_nextcloud_url_async.delay(user.id)
                return Response({
                    "auth_url": None,
                    "is_authenticated": False,
                    "error": str(e),
                    "message": "Provisioning your Nextcloud account in the background."
                }, status=status.HTTP_202_ACCEPTED)

        elif service == "onlyoffice":
            onlyoffice_url = os.getenv(
                "ONLYOFFICE_URL",
                "https://onlyoffice.apps.datasqan.com"
            )
            return Response({
                "auth_url": onlyoffice_url,
                "is_authenticated": False,
                "user_email": user.email,
            })

        return Response({
            "error": f"Connector '{service}' is not yet implemented.",
            "is_authenticated": False,
        }, status=status.HTTP_501_NOT_IMPLEMENTED)


class ConnectorAuthStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, service):
        user = request.user

        if service == "nextcloud":
            from oauth2_provider.models import Application
            has_app = Application.objects.filter(
                user=user,
                name="nextcloud_oidc"
            ).exists()

            connection_type = "managed"
            try:
                if hasattr(user, 'nextcloud_connection'):
                    connection_type = user.nextcloud_connection.connection_type
            except Exception:
                pass

            return Response({
                "is_authenticated": has_app or connection_type == "custom",
                "connection_type": connection_type,
                "user_email": user.email if has_app else None,
                "custom_server_url": user.nextcloud_connection.custom_server_url if connection_type == "custom" and hasattr(user, 'nextcloud_connection') else None,
            })

        elif service == "onlyoffice":
            return Response({
                "is_authenticated": True,
                "user_email": user.email,
            })

        return Response({
            "is_authenticated": False,
            "error": f"Connector '{service}' is not yet implemented.",
        })


class ConnectorAuthRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, service):
        user = request.user

        if service == "nextcloud":
            from oauth2_provider.models import Application
            deleted, _ = Application.objects.filter(
                user=user,
                name="nextcloud_oidc"
            ).delete()

            IntegrationLog.objects.create(
                user=user,
                connector="nextcloud",
                status="skipped",
                details="Nextcloud OIDC app revoked"
            )

            return Response({
                "success": True,
                "message": "Nextcloud authentication revoked."
            })

        elif service == "onlyoffice":
            return Response({
                "success": True,
                "message": "OnlyOffice does not require authentication."
            })

        return Response({
            "success": False,
            "error": f"Connector '{service}' is not yet implemented.",
        }, status=status.HTTP_501_NOT_IMPLEMENTED)


class ConnectorConnectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, service):
        """
        Establish a connection to a connector service.
        For Nextcloud:
          - "managed": provisions an auto-managed account via OIDC (existing flow).
          - "custom": validates and stores user-provided Nextcloud credentials.
        """
        if service == "nextcloud":
            user = request.user
            connection_type = request.data.get("connection_type", "managed")

            if connection_type == "managed":
                from oauth2_provider.models import Application
                from integrations.oidc_utils import get_or_create_nextcloud_oidc_user, generate_nextcloud_oidc_url

                has_app = Application.objects.filter(user=user, name="nextcloud_oidc").exists()

                if has_app:
                    NextcloudConnection.objects.update_or_create(
                        user=user,
                        defaults={"connection_type": "managed"},
                    )
                    return Response({
                        "connection_type": "managed",
                        "auth_url": None,
                        "is_authenticated": True,
                        "user_email": user.email,
                    })

                try:
                    get_or_create_nextcloud_oidc_user(user)
                    url = generate_nextcloud_oidc_url(user)

                    IntegrationLog.objects.create(
                        user=user,
                        connector="nextcloud",
                        status="processing",
                        details=f"OIDC login URL generated for user {user.id} via managed connection"
                    )

                    NextcloudConnection.objects.update_or_create(
                        user=user,
                        defaults={"connection_type": "managed"},
                    )

                    return Response({
                        "connection_type": "managed",
                        "auth_url": url,
                        "is_authenticated": False,
                        "user_email": user.email,
                    })
                except Exception as e:
                    generate_nextcloud_url_async.delay(user.id)
                    return Response({
                        "connection_type": "managed",
                        "auth_url": None,
                        "is_authenticated": False,
                        "error": str(e),
                        "message": "Provisioning your Nextcloud account in the background."
                    }, status=status.HTTP_202_ACCEPTED)

            elif connection_type == "custom":
                server_url = request.data.get("server_url", "").rstrip("/")
                username = request.data.get("username", "")
                password = request.data.get("password", "")

                if not server_url or not username or not password:
                    return Response({
                        "error": "server_url, username, and password are required.",
                        "connection_type": "custom",
                    }, status=status.HTTP_400_BAD_REQUEST)

                try:
                    nc_test_connection(server_url, username, password)

                    NextcloudConnection.objects.update_or_create(
                        user=user,
                        defaults={
                            "connection_type": "custom",
                            "custom_server_url": server_url,
                            "custom_username": username,
                            "custom_password": password,
                        },
                    )

                    IntegrationLog.objects.create(
                        user=user,
                        connector="nextcloud",
                        status="success",
                        details=f"Custom Nextcloud connection saved for {username} @ {server_url}"
                    )

                    return Response({
                        "connection_type": "custom",
                        "is_authenticated": True,
                        "user_email": username,
                        "server_url": server_url,
                    })

                except PermissionError as e:
                    return Response({
                        "connection_type": "custom",
                        "is_authenticated": False,
                        "error": str(e),
                    }, status=status.HTTP_401_UNAUTHORIZED)

                except Exception as e:
                    return Response({
                        "connection_type": "custom",
                        "is_authenticated": False,
                        "error": f"Connection failed: {e}",
                    }, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                "error": f"Invalid connection_type '{connection_type}'.",
            }, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "error": f"Connector '{service}' is not yet implemented.",
        }, status=status.HTTP_501_NOT_IMPLEMENTED)


class ConnectorFilesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, service):
        if service == "nextcloud":
            try:
                folder_path = request.query_params.get("folder_id", "")
                files = nc_list_files(request.user, folder_path)
                return Response({
                    "files": files,
                    "has_more": False,
                })
            except Exception as e:
                return Response({
                    "files": [],
                    "has_more": False,
                    "error": str(e),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        elif service == "onlyoffice":
            return Response({
                "files": [],
                "has_more": False,
                "message": "OnlyOffice is a document editor. Open documents from aiDocuMines to edit them in OnlyOffice."
            })

        return Response({
            "error": f"Connector '{service}' is not yet implemented.",
        }, status=status.HTTP_501_NOT_IMPLEMENTED)


class ConnectorImportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, service):
        """
        Import (pull) selected files from Nextcloud into aiDocuMines via the shared upload pipeline.
        Expects JSON: { "file_ids": [...], "project_id": "...", "service_id": "..." }
        Files are saved using the same path structure as the regular upload endpoint:
          {MEDIA_ROOT}/uploads/{client}/{user}/{project_id}/{service_id}/{YYYYMMDD}/{HHMMSS}_{filename}
        """
        if service == "nextcloud":
            from core.models import Run
            from core.utils import sanitize_filename
            from core.views import finalize_uploaded_file
            from core.tasks import process_bulk_metadata
            from platform_data_insights.models import UserInsights
            from platform_data_insights.tasks import generate_insights_for_user
            from oauth2_provider.models import Application
            from datetime import datetime
            import tempfile

            user = request.user
            file_ids = request.data.get("file_ids", [])
            project_id = request.data.get("project_id", "default")
            service_id = request.data.get("service_id", "nextcloud")

            if not file_ids:
                return Response({"error": "file_ids is required"}, status=status.HTTP_400_BAD_REQUEST)

            run = Run.objects.create(user=user, status="Pending")

            oauth_app = Application.objects.filter(user=user).first()
            client_id = oauth_app.client_id if oauth_app else str(user.id)
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

            results = []
            successful = 0
            failed = 0

            for nc_path in file_ids:
                try:
                    content = nc_download_file(user, nc_path)
                    filename = nc_path.split("/")[-1] if "/" in nc_path else nc_path
                    filename = sanitize_filename(filename)

                    with tempfile.NamedTemporaryFile(delete=False, suffix="_" + filename) as tmp:
                        tmp.write(content)
                        tmp_path = tmp.name

                    fp = finalize_uploaded_file(
                        user=user,
                        run=run,
                        project_id=project_id,
                        service_id=service_id,
                        file_path=tmp_path,
                        filename=filename,
                        client_id=client_id,
                        folder_name=None,
                        timestamp=timestamp,
                    )

                    results.append({
                        "external_id": nc_path,
                        "file_id": fp["file_id"],
                        "filename": filename,
                        "status": "success",
                    })
                    successful += 1

                except Exception as e:
                    results.append({
                        "external_id": nc_path,
                        "file_id": None,
                        "filename": nc_path.split("/")[-1] if "/" in nc_path else nc_path,
                        "status": "failed",
                        "error": str(e),
                    })
                    failed += 1

            UserInsights.objects.filter(user=user).delete()
            generate_insights_for_user.delay(user.id)
            process_bulk_metadata.delay(str(run.run_id))

            return Response({
                "run_id": str(run.run_id),
                "files": results,
                "total_files": len(file_ids),
                "successful": successful,
                "failed": failed,
            })

        return Response({
            "error": f"Connector '{service}' is not yet implemented.",
        }, status=status.HTTP_501_NOT_IMPLEMENTED)


class ConnectorExportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, service):
        """
        Export (push) aiDocuMines files to Nextcloud.
        Expects JSON: { "file_ids": ["<uuid>", ...], "target_folder": "..." }
        """
        if service == "nextcloud":
            from core.models import File as AiFile

            user = request.user
            file_ids = request.data.get("file_ids", [])
            target_folder = request.data.get("target_folder", "aiDocuMines Import")

            if not file_ids:
                return Response({"error": "file_ids is required"}, status=status.HTTP_400_BAD_REQUEST)

            nc_ensure_folder(user, target_folder)

            results = []
            successful = 0
            failed = 0

            for file_id in file_ids:
                try:
                    ai_file = AiFile.objects.get(id=file_id)
                    file_path = ai_file.filepath
                    if not file_path or not os.path.isfile(file_path):
                        raise ValueError("File has no stored content")

                    dest = f"{target_folder}/{ai_file.filename}"
                    with open(file_path, "rb") as f:
                        content = f.read()
                    nc_upload_file(user, dest, content)

                    results.append({
                        "file_id": str(ai_file.id),
                        "filename": ai_file.filename,
                        "status": "success",
                    })
                    successful += 1

                except AiFile.DoesNotExist:
                    results.append({
                        "file_id": file_id,
                        "filename": None,
                        "status": "failed",
                        "error": "File not found",
                    })
                    failed += 1
                except Exception as e:
                    results.append({
                        "file_id": file_id,
                        "filename": None,
                        "status": "failed",
                        "error": str(e),
                    })
                    failed += 1

            return Response({
                "run_id": f"export_{user.id}_{int(time.time())}",
                "files": results,
                "total_files": len(file_ids),
                "successful": successful,
                "failed": failed,
            })

        return Response({
            "error": f"Connector '{service}' is not yet implemented.",
        }, status=status.HTTP_501_NOT_IMPLEMENTED)


class NextcloudRedirectView(LoginRequiredMixin, View):
    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get(self, request):
        user = request.user
        try:
            state = secrets.token_urlsafe(16)
            nonce = secrets.token_urlsafe(16)

            STATE_REGISTRY[user.id] = state
            NONCE_REGISTRY[user.id] = nonce

            url = generate_nextcloud_oidc_url(user)
            return redirect(url)

        except Exception as e:
            IntegrationLog.objects.create(
                user=user,
                connector="nextcloud",
                status="error",
                details=f"Redirect error: {str(e)}"
            )
            return HttpResponseServerError(f"Nextcloud autologin failed: {str(e)}")


class NextcloudAutologinView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        try:
            get_or_create_nextcloud_oidc_user(user)

            state = secrets.token_urlsafe(16)
            nonce = secrets.token_urlsafe(16)

            STATE_REGISTRY[user.id] = state
            NONCE_REGISTRY[user.id] = nonce

            url = generate_nextcloud_oidc_url(user)
            return Response({"nextcloud_url": url})

        except Exception as e:
            IntegrationLog.objects.create(
                user=user,
                connector="nextcloud",
                status="processing",
                details=f"Nextcloud OIDC fallback triggered for {user.id}: {str(e)}"
            )

            generate_nextcloud_url_async.delay(user.id)
            sync_user_to_nextcloud_host.delay(user.id, user.username)

            return Response({
                "message": "We’re setting up your Nextcloud account.",
                "error": str(e)
            }, status=status.HTTP_202_ACCEPTED)


class IntegrationLogListView(generics.ListAPIView):
    queryset = IntegrationLog.objects.select_related('user').order_by('-timestamp')
    serializer_class = IntegrationLogSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['user__email', 'user__username', 'connector', 'status', 'details']
    ordering_fields = ['timestamp', 'status', 'connector']


class SyncUserToNextcloudView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        user_id = request.data.get("user_id")
        if not user_id:
            return Response({"error": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        User = get_user_model()
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        from .tasks import provision_nextcloud_user
        provision_nextcloud_user.delay(user.id)
        return Response({"message": f"Provisioning queued for user {user.id} ({user.email})"})


class CustomOIDCMetadataView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        base_url = os.getenv("API_BASE_URL", "https://aidocumines-api-layer.apps.datasqan.com") + "/o"
        return Response({
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/authorize/",
            "token_endpoint": f"{base_url}/token/",
            "userinfo_endpoint": f"{base_url}/userinfo/",
            "jwks_uri": f"{base_url}/.well-known/jwks.json",
            "scopes_supported": ["openid", "profile", "email", "read", "write"],
            "response_types_supported": [
                "code", "token", "id_token", "id_token token",
                "code token", "code id_token", "code id_token token"
            ],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256", "HS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["plain", "S256"],
            "claims_supported": ["sub"]
        })

