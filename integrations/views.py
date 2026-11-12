# integrations/views.py

import os
import secrets
import time
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
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from django.contrib.auth import get_user_model

from integrations.oidc_utils import get_or_create_nextcloud_oidc_user, generate_nextcloud_oidc_url
from integrations.tasks import generate_nextcloud_url_async, sync_user_to_nextcloud_host
from integrations.models import IntegrationLog
from .serializers import IntegrationLogSerializer

from integrations.registry import STATE_REGISTRY, NONCE_REGISTRY


class ConnectorAuthInitiateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, service):
        user = request.user

        if service == "nextcloud":
            try:
                get_or_create_nextcloud_oidc_user(user)

                state = secrets.token_urlsafe(16)
                nonce = secrets.token_urlsafe(16)

                STATE_REGISTRY[user.id] = state
                NONCE_REGISTRY[user.id] = nonce

                url = generate_nextcloud_oidc_url(user, state=state, nonce=nonce)

                IntegrationLog.objects.create(
                    user=user,
                    connector="nextcloud",
                    status="processing",
                    details=f"OIDC autologin URL generated for user {user.id}"
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

            return Response({
                "is_authenticated": has_app,
                "user_email": user.email if has_app else None,
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


class ConnectorFilesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, service):
        if service == "nextcloud":
            return Response({
                "files": [],
                "has_more": False,
                "message": "Use the Nextcloud web interface to browse files. Synced aiDocuMines data appears in your Nextcloud files."
            })

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
        if service == "nextcloud":
            from .tasks import sync_user_to_nextcloud_host
            client_name = ""
            if hasattr(request.user, 'client') and request.user.client:
                client_name = request.user.client.name
            sync_user_to_nextcloud_host.delay(request.user.id, client_name)
            return Response({
                "run_id": f"sync_{request.user.id}_{int(time.time())}",
                "files": [],
                "total_files": 0,
                "successful": 0,
                "failed": 0,
                "message": "Sync initiated. Check back shortly."
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

            url = generate_nextcloud_oidc_url(user, state=state, nonce=nonce)
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
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request):
        user = request.user
        try:
            get_or_create_nextcloud_oidc_user(user)

            state = secrets.token_urlsafe(16)
            nonce = secrets.token_urlsafe(16)

            STATE_REGISTRY[user.id] = state
            NONCE_REGISTRY[user.id] = nonce

            url = generate_nextcloud_oidc_url(user, state=state, nonce=nonce)
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

