from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from oauth2_provider.models import Application
from core.models import File
from document_workflows.models import Workflow, WorkflowStep, WorkflowAssignment, WorkflowRun, WorkflowAuditLog
from document_workflows.serializers import (
    WorkflowSerializer,
    WorkflowStepSerializer,
    WorkflowAssignmentSerializer,
    WorkflowRunSerializer,
    WorkflowAuditLogSerializer,
)
from document_workflows.tasks import execute_workflow_run_task
from document_workflows.utils import advance_workflow, log_audit, calculate_sla_deadline
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import logging
import os
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner, client_name_q


def _workflows(request):
    return Workflow.objects.filter(client_name_q(request.user))


def _workflow_runs(request):
    return WorkflowRun.objects.filter(client_name_q(request.user))


logger = logging.getLogger(__name__)

client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID for authentication"
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client Secret for authentication"
)
workflow_id_param = openapi.Parameter(
    "workflow_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique Workflow ID"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Unique File ID"
)
run_id_param = openapi.Parameter(
    "run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique workflow run ID"
)
step_assignment_id_param = openapi.Parameter(
    "step_assignment_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Step assignment ID"
)
action_param = openapi.Parameter(
    "action", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Action: approve or reject"
)


def health_check(request):
    from django.http import JsonResponse
    return JsonResponse({"status": "ok"}, status=200)


def get_user_from_client_id(client_id):
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


class WorkflowListCreateAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all workflows.",
        tags=["Workflows"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request):
        workflows = _workflows(request)
        serializer = WorkflowSerializer(workflows, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Create a new workflow.",
        tags=["Workflows"],
        manual_parameters=[client_id_param, client_secret_param],
        request_body=WorkflowSerializer,
    )
    def post(self, request):
        serializer = WorkflowSerializer(data=request.data)
        if serializer.is_valid():
            # Owner is always the authenticated user, never a value from the request.
            serializer.save(client_name=request.user.username or request.user.email)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class WorkflowDetailAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get workflow details by ID.",
        tags=["Workflows"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request, pk):
        workflow = get_object_or_404(_workflows(request), id=pk)
        serializer = WorkflowSerializer(workflow)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Update a workflow.",
        tags=["Workflows"],
        manual_parameters=[client_id_param, client_secret_param],
        request_body=WorkflowSerializer,
    )
    def put(self, request, pk):
        workflow = get_object_or_404(_workflows(request), id=pk)
        serializer = WorkflowSerializer(workflow, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save(client_name=workflow.client_name)
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class WorkflowStepListAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all workflow steps.",
        tags=["Workflow Steps"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request):
        steps = WorkflowStep.objects.filter(workflow__in=_workflows(request))
        serializer = WorkflowStepSerializer(steps, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Create a workflow step.",
        tags=["Workflow Steps"],
        manual_parameters=[client_id_param, client_secret_param],
        request_body=WorkflowStepSerializer,
    )
    def post(self, request):
        serializer = WorkflowStepSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class WorkflowAssignmentListAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all workflow assignments.",
        tags=["Workflow Assignments"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request):
        assignments = WorkflowAssignment.objects.filter(workflow__in=_workflows(request))
        serializer = WorkflowAssignmentSerializer(assignments, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class SubmitWorkflowRunAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Submit a workflow run with file_id and workflow_id.",
        tags=["Workflow Run"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param, workflow_id_param],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        workflow_id = request.query_params.get("workflow_id")

        if not all([client_id, client_secret, file_id, workflow_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        file_instance = get_file_for_user(request.user, file_id)
        workflow = get_object_or_404(_workflows(request), id=workflow_id)

        if file_instance.user != user:
            raise PermissionDenied("You are not authorized to use this file.")

        if not os.path.exists(file_instance.filepath):
            return Response({"error": "File not found."}, status=status.HTTP_404_NOT_FOUND)

        first_step = WorkflowStep.objects.filter(workflow=workflow).order_by('step_number').first()
        if not first_step:
            return Response({"error": "Workflow has no steps defined."}, status=status.HTTP_400_BAD_REQUEST)

        actor = user.username if user and user.username else user.email

        sla_deadline = calculate_sla_deadline(first_step) if first_step.sla_hours else None

        workflow_run = WorkflowRun.objects.create(
            workflow=workflow,
            project_id=file_instance.project_id,
            service_id=file_instance.service_id,
            client_name=actor,
            status="Pending",
            file=file_instance,
            current_step=first_step,
            sla_deadline=sla_deadline,
        )

        log_audit(workflow_run, 'created', actor)

        first_step.status = "InProgress"
        first_step.save()

        execute_workflow_run_task.delay(str(workflow_run.id))

        response_data = WorkflowRunSerializer(workflow_run).data
        return Response(response_data, status=status.HTTP_202_ACCEPTED)


class CheckWorkflowStatusAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Check the status of a workflow run.",
        tags=["Workflow Run"],
        manual_parameters=[client_id_param, client_secret_param, run_id_param],
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")

        if not run_id:
            return Response({"error": "Missing `run_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        workflow_run = get_object_or_404(_workflow_runs(request), id=run_id)
        serializer = WorkflowRunSerializer(workflow_run)

        sla_info = {}
        if workflow_run.sla_deadline:
            from django.utils import timezone
            sla_info = {
                "sla_deadline": workflow_run.sla_deadline.isoformat(),
                "sla_breached": timezone.now() > workflow_run.sla_deadline,
            }

        response_data = serializer.data
        response_data["sla_info"] = sla_info

        return Response(response_data, status=status.HTTP_200_OK)


class ApproveStepAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Approve or reject a workflow step assignment.",
        tags=["Workflow Run"],
        manual_parameters=[client_id_param, client_secret_param, step_assignment_id_param, action_param],
    )
    def post(self, request):
        step_assignment_id = request.query_params.get("step_assignment_id")
        action = request.query_params.get("action", "").lower()

        if not step_assignment_id or action not in ("approve", "reject"):
            return Response({"error": "Missing or invalid parameters"}, status=status.HTTP_400_BAD_REQUEST)

        assignment = get_object_or_404(
            WorkflowAssignment.objects.filter(workflow__in=_workflows(request)), id=step_assignment_id
        )

        client_id = request.headers.get("X-Client-ID")
        actor = request.user.username or request.user.email

        run = WorkflowRun.objects.filter(workflow=assignment.workflow).first()

        if action == "approve":
            assignment.status = "Approved"
            log_audit(run, 'approved', actor, step=assignment.step, details={
                'assignment_id': str(assignment.id),
            })
        else:
            assignment.status = "Rejected"
            log_audit(run, 'rejected', actor, step=assignment.step, details={
                'assignment_id': str(assignment.id),
            })

        assignment.save()

        if run:
            advance_workflow(str(run.id))

        return Response({
            "step_assignment_id": str(assignment.id),
            "status": assignment.status,
        }, status=status.HTTP_200_OK)


class WorkflowAuditLogListView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List audit logs for a workflow run.",
        tags=["Workflow Audit"],
        manual_parameters=[client_id_param, client_secret_param, run_id_param],
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")
        if not run_id:
            return Response({"error": "Missing `run_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        audit_logs = WorkflowAuditLog.objects.filter(run__in=_workflow_runs(request), run_id=run_id)
        serializer = WorkflowAuditLogSerializer(audit_logs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class WorkflowRunSLAUpdateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Update SLA settings on a workflow run.",
        tags=["Workflow Run"],
        manual_parameters=[client_id_param, client_secret_param],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'run_id': openapi.Schema(type=openapi.TYPE_STRING, description='Workflow run ID'),
                'sla_deadline': openapi.Schema(type=openapi.TYPE_STRING, description='New SLA deadline (ISO format)'),
            },
            required=['run_id'],
        ),
    )
    def put(self, request):
        run_id = request.data.get("run_id")
        sla_deadline = request.data.get("sla_deadline")

        if not run_id:
            return Response({"error": "Missing `run_id` in request body"}, status=status.HTTP_400_BAD_REQUEST)

        workflow_run = get_object_or_404(_workflow_runs(request), id=run_id)

        from datetime import datetime
        from django.utils.dateparse import parse_datetime

        if sla_deadline:
            parsed = parse_datetime(sla_deadline)
            if parsed:
                workflow_run.sla_deadline = parsed
            else:
                return Response({"error": "Invalid datetime format. Use ISO format."}, status=status.HTTP_400_BAD_REQUEST)

        workflow_run.save()

        client_id = request.headers.get("X-Client-ID")
        actor = request.user.username or request.user.email

        log_audit(workflow_run, 'sla_updated', actor, details={
            'sla_deadline': sla_deadline,
        })

        serializer = WorkflowRunSerializer(workflow_run)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AddCommentToStepView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Add a comment to a workflow assignment.",
        tags=["Workflow Assignments"],
        manual_parameters=[client_id_param, client_secret_param],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'assignment_id': openapi.Schema(type=openapi.TYPE_STRING, description='Assignment ID'),
                'comment': openapi.Schema(type=openapi.TYPE_STRING, description='Comment text'),
            },
            required=['assignment_id', 'comment'],
        ),
    )
    def post(self, request):
        assignment_id = request.data.get("assignment_id")
        comment = request.data.get("comment")

        if not assignment_id or not comment:
            return Response({"error": "Missing `assignment_id` or `comment`"}, status=status.HTTP_400_BAD_REQUEST)

        assignment = get_object_or_404(
            WorkflowAssignment.objects.filter(workflow__in=_workflows(request)), id=assignment_id
        )

        existing_comments = assignment.comments or ""
        if existing_comments:
            assignment.comments = existing_comments + "\n---\n" + comment
        else:
            assignment.comments = comment
        assignment.save()

        client_id = request.headers.get("X-Client-ID")
        actor = request.user.username or request.user.email

        run = WorkflowRun.objects.filter(workflow=assignment.workflow).first()
        log_audit(run, 'comment_added', actor, step=assignment.step, details={
            'assignment_id': str(assignment.id),
            'comment': comment,
        })

        return Response({
            "assignment_id": str(assignment.id),
            "comments": assignment.comments,
        }, status=status.HTTP_200_OK)
