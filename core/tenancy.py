from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView

from core import access


class ClientScopedViewMixin(APIView):
    """
    Scopes queryset lookups to what the authenticated user may access.

    Delegates to `core.access` so every view shares one policy:
      * core.File and file-derived artefacts -> owner, explicit shares, tenant admins
      * processing runs (OCR / translation / anonymisation) -> creator, tenant admins,
        anyone with access to the run's files
      * any other model -> rows whose `client_scope_field` matches the user's tenant
    Platform superusers see everything. Inaccessible objects raise 404.
    """

    client_scope_field = "user__client"

    # Path from a model to core.File for artefacts whose access derives from a file.
    FILE_RELATIONS = {
        "File": "",
        "OCRFile": "original_file",
        "TranslationFile": "original_file",
        "Anonymize": "original_file",
        "DeAnonymize": "file",
    }

    def get_client(self):
        return getattr(self.request.user, "client", None)

    def _scope(self, queryset):
        user = self.request.user
        name = queryset.model.__name__
        if name in self.FILE_RELATIONS:
            return access.scope_by_file(user, queryset, self.FILE_RELATIONS[name]) \
                if self.FILE_RELATIONS[name] else access.accessible_files(user, "view", queryset)
        if name in access.RUN_FILE_RELATIONS:
            return access.scope_runs(user, queryset)
        if access.is_platform_superuser(user):
            return queryset
        client = self.get_client()
        if client is None:
            return queryset.none()
        return queryset.filter(**{self.client_scope_field: client})

    def get_client_scoped_queryset(self, queryset):
        return self._scope(queryset)

    def get_object_by_client(self, queryset, **lookup_kwargs):
        try:
            return get_object_or_404(self._scope(queryset), **lookup_kwargs)
        except (ValueError, TypeError):
            raise Http404("Not found.")
