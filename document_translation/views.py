from django.db import models
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application
from core.models import File
from core.utils import get_logical_path
from document_translation.models import TranslationRun, TranslationFile, TranslationLanguage, TranslationStorage

from document_translation.tasks import (
    translate_document_task,
    check_translation_status_task,
    download_translated_file_task
)
from core.tenancy import ClientScopedViewMixin
from custom_authentication.permissions import IsAdminOrManagerMutation
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import logging
import os
from rest_framework.permissions import AllowAny
from rest_framework import generics
from document_translation.serializers import TranslationFileSerializer

from django.http import FileResponse, Http404
from django.utils.encoding import smart_str


logger = logging.getLogger(__name__)

User = get_user_model()

# ✅ Define Swagger parameters
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID for authentication"
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client Secret for authentication"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Unique File ID"
)
translation_run_id_param = openapi.Parameter(
    "translation_run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique translation_run_id to track translation."
)
from_language_param = openapi.Parameter(
    "from_language", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Source language code"
)
to_language_param = openapi.Parameter(
    "to_language", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Target language code"
)


def health_check(request):
    from django.http import JsonResponse
    return JsonResponse({"status": "ok"}, status=200)

def get_user_from_client_id(client_id):
    """Retrieves the User associated with a given `client_id` from OAuth2 Application."""
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


### **🔹 Submit Translation**
class SubmitTranslationAPIView(ClientScopedViewMixin):
    """
    Submit a file for translation and track using `file_id` and `translation_run_id`.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsAdminOrManagerMutation]

    @swagger_auto_schema(
        operation_description="Submit a file for translation.",
        tags=["Translation"],
        manual_parameters=[
            client_id_param, client_secret_param, file_id_param, from_language_param, to_language_param
        ],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        from_lang = request.query_params.get("from_language")
        to_lang = request.query_params.get("to_language")

        if not all([client_id, client_secret, file_id, from_lang, to_lang]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Get user from client_id
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        # ✅ Get the file instance scoped by client
        file_instance = self.get_object_by_client(File.objects.all(), id=file_id)

        if file_instance.user != user:
            raise PermissionDenied("You are not authorized to translate this file.")

        if not os.path.exists(file_instance.filepath):
            return Response({"error": "File not found."}, status=status.HTTP_404_NOT_FOUND)

        # ✅ Validate source and target languages
        if from_lang != "auto" and not TranslationLanguage.objects.filter(code=from_lang).exists():
            return Response({"error": f"Invalid source language: {from_lang}"}, status=status.HTTP_400_BAD_REQUEST)
        if not TranslationLanguage.objects.filter(code=to_lang).exists():
            return Response({"error": f"Invalid target language: {to_lang}"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Create a new translation run with client FK
        translation_run = TranslationRun.objects.create(
            project_id=file_instance.project_id,
            service_id=file_instance.service_id,
            from_language=from_lang,
            to_language=to_lang,
            status="Processing",
            client=request.user.client,
            client_name=user.username if user and user.username else user.email
        )

        translate_document_task.delay(file_id, str(translation_run.id), from_lang, to_lang)

        response_data = {
            "translation_run_id": str(translation_run.id),
            "file_id": str(file_instance.id),
            "status": "Processing"
        }

        return Response(response_data, status=status.HTTP_202_ACCEPTED)


### **🔹 Check Translation Status**
class CheckTranslationStatusAPIView(ClientScopedViewMixin):
    """
    Check the translation status using `translation_run_id`.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]
    client_scope_field = "client"

    @swagger_auto_schema(
        operation_description="Check the status of a document translation using `translation_run_id`.",
        tags=["Translation Status"],
        manual_parameters=[client_id_param, client_secret_param, translation_run_id_param],
    )
    def get(self, request):
        translation_run_id = request.query_params.get("translation_run_id")

        if not translation_run_id:
            return Response({"error": "Missing `translation_run_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Verify translation run belongs to user's client
        self.get_object_by_client(TranslationRun.objects.all(), id=translation_run_id)

        # ✅ Fetch translation status asynchronously
        result = check_translation_status_task.delay(translation_run_id).get()

        return Response(result, status=status.HTTP_200_OK if result["status"] == "Completed" else status.HTTP_202_ACCEPTED)


### **🔹 Download Translated File**
class DownloadTranslatedFileAPIView(ClientScopedViewMixin):
    """
    Download the translated file.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Download a translated document by providing the `file_id`.",
        tags=["Translation File Download"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param],
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")

        if not file_id:
            return Response({"error": "Missing `file_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Verify file belongs to user's client
        self.get_object_by_client(File.objects.all(), id=file_id)

        # ✅ Fetch translated file path asynchronously
        result = download_translated_file_task.delay(file_id).get()

        if "error" in result:
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        translated_filepath = result["translated_filepath"]

        if not os.path.exists(translated_filepath):
            return Response({"error": "Translated file not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {"file_id": file_id, "translated_filepath": translated_filepath, "status": "Ready for download"},
            status=status.HTTP_200_OK
        )


### **🔹 Handle Translation File Download View (Fix Swagger Errors)**
class TranslationFileDownloadView(generics.RetrieveAPIView):
    queryset = TranslationFile.objects.all()
    serializer_class = TranslationFileSerializer
    permission_classes = [AllowAny]  # ✅ Ensure API can be accessed

    @swagger_auto_schema(auto_schema=None)  # ✅ Prevent Swagger from generating schema errors
    def get(self, request, *args, **kwargs):
        if getattr(self, 'swagger_fake_view', False):
            return None  # ✅ Short-circuit schema generation for Swagger
        return super().get(request, *args, **kwargs)





##################################################

### 🔹 TranslationRunSummaryView
class TranslationRunSummaryView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]
    client_scope_field = "client"

    @swagger_auto_schema(
        operation_description="Get summary of a translation run.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param, translation_run_id_param],
    )
    def get(self, request):
        run_id = request.query_params.get("translation_run_id")
        if not run_id:
            return Response({"error": "Missing translation_run_id"}, status=400)

        run = self.get_object_by_client(TranslationRun.objects.all(), id=run_id)
        file_count = run.translation_files.count()

        return Response({
            "run_id": str(run.id),
            "from_language": run.from_language,
            "to_language": run.to_language,
            "status": run.status,
            "error_message": run.error_message,
            "created_at": run.created_at,
            "files_translated": file_count
        })


### 🔹 TranslatedFilesByRunView
class TranslatedFilesByRunView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]
    client_scope_field = "client"

    @swagger_auto_schema(
        operation_description="List all translated files for a given translation run.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param, translation_run_id_param],
    )
    def get(self, request):
        run_id = request.query_params.get("translation_run_id")
        if not run_id:
            return Response({"error": "Missing translation_run_id"}, status=400)

        run = self.get_object_by_client(TranslationRun.objects.all(), id=run_id)
        files = run.translation_files.all()

        return Response([
            {
                "file_id": str(f.original_file.id),
                "original_file": f.original_file.filename,
                "translated_filepath": f.translated_filepath,
                "status": f.status
            } for f in files
        ])


### 🔹 TranslationFileInsightView
class TranslationFileInsightView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get insight about a translated file.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param, file_id_param],
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)

        translation_file = get_object_or_404(TranslationFile, original_file__id=file_id)
        self.get_object_by_client(File.objects.all(), id=translation_file.original_file.id)
        run = translation_file.run

        return Response({
            "file_id": file_id,
            "original_filepath": translation_file.original_filepath,
            "translated_filepath": translation_file.translated_filepath,
            "status": translation_file.status,
            "from_language": run.from_language,
            "to_language": run.to_language,
            "run_id": str(run.id)
        })


### 🔹 TranslationHistoryView
class TranslationHistoryView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all translations performed for a given original file.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param, file_id_param],
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)

        self.get_object_by_client(File.objects.all(), id=file_id)
        translations = TranslationFile.objects.filter(original_file__id=file_id)
        return Response([
            {
                "translated_filepath": t.translated_filepath,
                "status": t.status,
                "to_language": t.run.to_language,
                "created_at": t.created_at
            } for t in translations
        ])


### 🔹 SupportedLanguagesView
class SupportedLanguagesView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        operation_description="List all supported translation languages.",
        tags=["Translation Intelligence"],
    )
    def get(self, request):
        languages = TranslationLanguage.objects.all()
        return Response([
            {"name": lang.name, "code": lang.code} for lang in languages
        ])


### 🔹 TranslationStorageLocationsView
class TranslationStorageLocationsView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]
    client_scope_field = "client"

    @swagger_auto_schema(
        operation_description="Return upload and translated storage locations.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param, translation_run_id_param],
    )
    def get(self, request):
        run_id = request.query_params.get("translation_run_id")
        if not run_id:
            return Response({"error": "Missing translation_run_id"}, status=400)

        self.get_object_by_client(TranslationRun.objects.all(), id=run_id)
        storages = TranslationStorage.objects.filter(run__id=run_id)
        return Response([
            {
                "upload_storage_location": get_logical_path(s.upload_storage_location),
                "translated_storage_location": get_logical_path(s.translated_storage_location)
            } for s in storages
        ])


### 🔹 TranslationClientSummaryView
class TranslationClientSummaryView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Return summary of all translation activity for current user.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        files = TranslationFile.objects.filter(original_file__user=user)
        total_runs = TranslationRun.objects.filter(client_name=user.username).count()
        total_files = files.count()
        success_rate = round(files.filter(status="Completed").count() / total_files, 2) if total_files else 0

        lang_counts = files.values("run__to_language").annotate(count=models.Count("id"))
        top_languages = sorted([
            {"code": entry["run__to_language"], "count": entry["count"]} for entry in lang_counts
        ], key=lambda x: -x["count"])[:5]

        recent = files.order_by("-created_at")[:5]

        return Response({
            "total_translation_runs": total_runs,
            "total_files_translated": total_files,
            "success_rate": success_rate,
            "top_languages": top_languages,
            "recent_translations": [
                {
                    "original_file": f.original_file.filename,
                    "to_language": f.run.to_language,
                    "status": f.status,
                    "date": f.created_at
                } for f in recent
            ]
        })


### 🔹 TranslationProjectSummaryView
class TranslationProjectSummaryView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]
    client_scope_field = "client"

    @swagger_auto_schema(
        operation_description="Aggregate translation data for a specific project.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param, openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)],
    )
    def get(self, request):
        project_id = request.query_params.get("project_id")
        if not project_id:
            return Response({"error": "Missing project_id"}, status=400)

        runs = self.get_client_scoped_queryset(TranslationRun.objects.filter(project_id=project_id))
        summary = {}
        for run in runs:
            lang = run.to_language
            summary[lang] = summary.get(lang, 0) + run.translation_files.count()

        return Response({"project_id": project_id, "language_summary": summary})


### 🔹 TranslationLanguageSummaryView
class TranslationLanguageSummaryView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Return stats for all files translated into a specific language.",
        tags=["Translation Intelligence"],
        manual_parameters=[client_id_param, openapi.Parameter("lang", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)],
    )
    def get(self, request):
        lang = request.query_params.get("lang")
        if not lang:
            return Response({"error": "Missing lang parameter"}, status=400)

        files = TranslationFile.objects.filter(
            run__to_language=lang,
            original_file__user__client=request.user.client
        )
        projects = files.values("original_file__project_id").distinct()

        return Response({
            "language": lang,
            "file_count": files.count(),
            "projects": [p["original_file__project_id"] for p in projects],
            "statuses": files.values("status").annotate(count=models.Count("id"))
        })

