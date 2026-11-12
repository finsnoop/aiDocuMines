# file_system/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from celery.result import AsyncResult
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from file_system.utils import get_user_file_tree
from file_system.tasks import generate_user_file_tree_task


class UserFileTreeView(APIView):
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request, *args, **kwargs):
        user_id = kwargs["user_id"]  # will be int because of <int:user_id>
        run_async = request.query_params.get("async", "false").lower() == "true"
        user = request.user

        if str(user.id) != str(user_id) and not user.is_superuser:
            return Response({"detail": "Permission denied"}, status=403)

        if run_async:
            task = generate_user_file_tree_task.delay(user.id)
            return Response({"message": "Task queued", "task_id": task.id}, status=202)

        return Response({"user_id": user.id, "structure": get_user_file_tree(user)})

