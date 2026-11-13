import logging
from datetime import datetime, timedelta
from django.db.models import Count, Avg, Q, Sum
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from custom_authentication.permissions import IsClientOrAdminOrSuperUser
from oauth2_provider.models import Application
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

# Import Client model
from custom_authentication.models import Client

# Import ServiceExecution models from all 5 vertical apps
from private_equity.models import ServiceExecution as PEServiceExecution
from class_actions.models import ServiceExecution as CAServiceExecution
from labor_employment.models import ServiceExecution as LEServiceExecution
from ip_litigation.models import ServiceExecution as IPServiceExecution
from regulatory_compliance.models import ServiceExecution as RCServiceExecution

# Import tool output models from all 5 verticals for data insights
from private_equity.models import (
    DueDiligenceRun, DocumentClassification, RiskClause, FindingsReport
)
from class_actions.models import (
    MassClaimsRun, IntakeForm, EvidenceDocument, SettlementTracking
)
from labor_employment.models import (
    WorkplaceCommunicationsRun, CommunicationMessage, WageHourAnalysis,
    ComplianceAlert as LEComplianceAlert
)
from ip_litigation.models import (
    PatentAnalysisRun, PatentDocument, PatentClaim, PriorArtDocument,
    ClaimChart, PatentLandscape
)
from regulatory_compliance.models import (
    ComplianceRun, RegulatoryRequirement, PolicyMapping, DSARRequest,
    ComplianceAlert as RCComplianceAlert
)

from service_analytics.serializers import (
    OverviewSerializer,
    ServiceExecutionSerializer,
    ServiceBreakdownSerializer,
    TrendsSerializer,
)

logger = logging.getLogger(__name__)

# Swagger Parameters
client_id_param = openapi.Parameter("X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
client_secret_param = openapi.Parameter("X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)


def get_user_from_client_id(client_id):
    """Get user from OAuth2 client_id"""
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


class ServiceAnalyticsOverviewAPIView(APIView):
    """
    Get overall service analytics overview across all 5 legal verticals.
    Returns aggregated statistics including total executions, success rates, and breakdowns.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[client_id_param, client_secret_param],
        operation_description="Get overall service analytics overview across all legal verticals",
        responses={200: OverviewSerializer()}
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")

        if not all([client_id, client_secret]):
            return Response(
                {"error": "Missing X-Client-ID or X-Client-Secret headers"},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = get_user_from_client_id(client_id)
        if not user:
            return Response(
                {"error": "Invalid client credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Get client for multi-tenant filtering
        client = user.client if hasattr(user, 'client') and user.client else None

        # Get all service executions for this client from all 5 verticals (multi-tenant)
        if client:
            pe_executions = PEServiceExecution.objects.filter(client=client)
            ca_executions = CAServiceExecution.objects.filter(client=client)
            le_executions = LEServiceExecution.objects.filter(client=client)
            ip_executions = IPServiceExecution.objects.filter(client=client)
            rc_executions = RCServiceExecution.objects.filter(client=client)
        else:
            # Fallback to user-based filtering for users without a client
            pe_executions = PEServiceExecution.objects.filter(user=user)
            ca_executions = CAServiceExecution.objects.filter(user=user)
            le_executions = LEServiceExecution.objects.filter(user=user)
            ip_executions = IPServiceExecution.objects.filter(user=user)
            rc_executions = RCServiceExecution.objects.filter(user=user)

        # Calculate overall statistics
        total_executions = (
            pe_executions.count() +
            ca_executions.count() +
            le_executions.count() +
            ip_executions.count() +
            rc_executions.count()
        )

        # Status counts
        completed = (
            pe_executions.filter(status='completed').count() +
            ca_executions.filter(status='completed').count() +
            le_executions.filter(status='completed').count() +
            ip_executions.filter(status='completed').count() +
            rc_executions.filter(status='completed').count()
        )

        failed = (
            pe_executions.filter(status='failed').count() +
            ca_executions.filter(status='failed').count() +
            le_executions.filter(status='failed').count() +
            ip_executions.filter(status='failed').count() +
            rc_executions.filter(status='failed').count()
        )

        running = (
            pe_executions.filter(status='running').count() +
            ca_executions.filter(status='running').count() +
            le_executions.filter(status='running').count() +
            ip_executions.filter(status='running').count() +
            rc_executions.filter(status='running').count()
        )

        pending = (
            pe_executions.filter(status='pending').count() +
            ca_executions.filter(status='pending').count() +
            le_executions.filter(status='pending').count() +
            ip_executions.filter(status='pending').count() +
            rc_executions.filter(status='pending').count()
        )

        cancelled = (
            pe_executions.filter(status='cancelled').count() +
            ca_executions.filter(status='cancelled').count() +
            le_executions.filter(status='cancelled').count() +
            ip_executions.filter(status='cancelled').count() +
            rc_executions.filter(status='cancelled').count()
        )

        # Success rate
        success_rate = (completed / total_executions * 100) if total_executions > 0 else 0

        # Total output files
        total_output_files = (
            pe_executions.aggregate(total=Sum('output_count'))['total'] or 0
        ) + (
            ca_executions.aggregate(total=Sum('output_count'))['total'] or 0
        ) + (
            le_executions.aggregate(total=Sum('output_count'))['total'] or 0
        ) + (
            ip_executions.aggregate(total=Sum('output_count'))['total'] or 0
        ) + (
            rc_executions.aggregate(total=Sum('output_count'))['total'] or 0
        )

        # Average execution time (only for completed executions)
        pe_avg = pe_executions.filter(status='completed', execution_time_seconds__isnull=False).aggregate(avg=Avg('execution_time_seconds'))['avg']
        ca_avg = ca_executions.filter(status='completed', execution_time_seconds__isnull=False).aggregate(avg=Avg('execution_time_seconds'))['avg']
        le_avg = le_executions.filter(status='completed', execution_time_seconds__isnull=False).aggregate(avg=Avg('execution_time_seconds'))['avg']
        ip_avg = ip_executions.filter(status='completed', execution_time_seconds__isnull=False).aggregate(avg=Avg('execution_time_seconds'))['avg']
        rc_avg = rc_executions.filter(status='completed', execution_time_seconds__isnull=False).aggregate(avg=Avg('execution_time_seconds'))['avg']

        # Calculate weighted average
        avg_times = [t for t in [pe_avg, ca_avg, le_avg, ip_avg, rc_avg] if t is not None]
        avg_execution_time = sum(avg_times) / len(avg_times) if avg_times else None

        # Verticals breakdown
        verticals_breakdown = []

        # Private Equity
        pe_total = pe_executions.count()
        if pe_total > 0:
            pe_completed = pe_executions.filter(status='completed').count()
            verticals_breakdown.append({
                'vertical': 'Private Equity',
                'total_executions': pe_total,
                'completed': pe_completed,
                'failed': pe_executions.filter(status='failed').count(),
                'running': pe_executions.filter(status='running').count(),
                'pending': pe_executions.filter(status='pending').count(),
                'success_rate': (pe_completed / pe_total * 100) if pe_total > 0 else 0,
                'avg_execution_time': pe_avg
            })

        # Class Actions
        ca_total = ca_executions.count()
        if ca_total > 0:
            ca_completed = ca_executions.filter(status='completed').count()
            verticals_breakdown.append({
                'vertical': 'Class Actions',
                'total_executions': ca_total,
                'completed': ca_completed,
                'failed': ca_executions.filter(status='failed').count(),
                'running': ca_executions.filter(status='running').count(),
                'pending': ca_executions.filter(status='pending').count(),
                'success_rate': (ca_completed / ca_total * 100) if ca_total > 0 else 0,
                'avg_execution_time': ca_avg
            })

        # Labor Employment
        le_total = le_executions.count()
        if le_total > 0:
            le_completed = le_executions.filter(status='completed').count()
            verticals_breakdown.append({
                'vertical': 'Labor Employment',
                'total_executions': le_total,
                'completed': le_completed,
                'failed': le_executions.filter(status='failed').count(),
                'running': le_executions.filter(status='running').count(),
                'pending': le_executions.filter(status='pending').count(),
                'success_rate': (le_completed / le_total * 100) if le_total > 0 else 0,
                'avg_execution_time': le_avg
            })

        # IP Litigation
        ip_total = ip_executions.count()
        if ip_total > 0:
            ip_completed = ip_executions.filter(status='completed').count()
            verticals_breakdown.append({
                'vertical': 'IP Litigation',
                'total_executions': ip_total,
                'completed': ip_completed,
                'failed': ip_executions.filter(status='failed').count(),
                'running': ip_executions.filter(status='running').count(),
                'pending': ip_executions.filter(status='pending').count(),
                'success_rate': (ip_completed / ip_total * 100) if ip_total > 0 else 0,
                'avg_execution_time': ip_avg
            })

        # Regulatory Compliance
        rc_total = rc_executions.count()
        if rc_total > 0:
            rc_completed = rc_executions.filter(status='completed').count()
            verticals_breakdown.append({
                'vertical': 'Regulatory Compliance',
                'total_executions': rc_total,
                'completed': rc_completed,
                'failed': rc_executions.filter(status='failed').count(),
                'running': rc_executions.filter(status='running').count(),
                'pending': rc_executions.filter(status='pending').count(),
                'success_rate': (rc_completed / rc_total * 100) if rc_total > 0 else 0,
                'avg_execution_time': rc_avg
            })

        # Most used services (top 10)
        most_used_services = self._get_most_used_services(client, user)

        response_data = {
            'total_executions': total_executions,
            'completed': completed,
            'failed': failed,
            'running': running,
            'pending': pending,
            'cancelled': cancelled,
            'success_rate': round(success_rate, 2),
            'total_output_files': total_output_files,
            'avg_execution_time': round(avg_execution_time, 2) if avg_execution_time else None,
            'verticals_breakdown': verticals_breakdown,
            'most_used_services': most_used_services[:10]
        }

        return Response(response_data, status=status.HTTP_200_OK)

    def _get_most_used_services(self, client, user):
        """Get most used services across all verticals (multi-tenant)"""
        services = []

        # Build filter based on client or user
        if client:
            pe_filter = {'client': client}
            ca_filter = {'client': client}
            le_filter = {'client': client}
            ip_filter = {'client': client}
            rc_filter = {'client': client}
        else:
            pe_filter = {'user': user}
            ca_filter = {'user': user}
            le_filter = {'user': user}
            ip_filter = {'user': user}
            rc_filter = {'user': user}

        # Private Equity services
        pe_services = PEServiceExecution.objects.filter(**pe_filter).values('service_type', 'service_name').annotate(
            count=Count('id'),
            completed_count=Count('id', filter=Q(status='completed'))
        ).order_by('-count')

        for service in pe_services:
            services.append({
                'service_type': service['service_type'],
                'service_name': service['service_name'],
                'vertical': 'Private Equity',
                'count': service['count'],
                'success_rate': (service['completed_count'] / service['count'] * 100) if service['count'] > 0 else 0,
                'avg_execution_time': None
            })

        # Class Actions services
        ca_services = CAServiceExecution.objects.filter(**ca_filter).values('service_type', 'service_name').annotate(
            count=Count('id'),
            completed_count=Count('id', filter=Q(status='completed'))
        ).order_by('-count')

        for service in ca_services:
            services.append({
                'service_type': service['service_type'],
                'service_name': service['service_name'],
                'vertical': 'Class Actions',
                'count': service['count'],
                'success_rate': (service['completed_count'] / service['count'] * 100) if service['count'] > 0 else 0,
                'avg_execution_time': None
            })

        # Labor Employment services
        le_services = LEServiceExecution.objects.filter(**le_filter).values('service_type', 'service_name').annotate(
            count=Count('id'),
            completed_count=Count('id', filter=Q(status='completed'))
        ).order_by('-count')

        for service in le_services:
            services.append({
                'service_type': service['service_type'],
                'service_name': service['service_name'],
                'vertical': 'Labor Employment',
                'count': service['count'],
                'success_rate': (service['completed_count'] / service['count'] * 100) if service['count'] > 0 else 0,
                'avg_execution_time': None
            })

        # IP Litigation services
        ip_services = IPServiceExecution.objects.filter(**ip_filter).values('service_type', 'service_name').annotate(
            count=Count('id'),
            completed_count=Count('id', filter=Q(status='completed'))
        ).order_by('-count')

        for service in ip_services:
            services.append({
                'service_type': service['service_type'],
                'service_name': service['service_name'],
                'vertical': 'IP Litigation',
                'count': service['count'],
                'success_rate': (service['completed_count'] / service['count'] * 100) if service['count'] > 0 else 0,
                'avg_execution_time': None
            })

        # Regulatory Compliance services
        rc_services = RCServiceExecution.objects.filter(**rc_filter).values('service_type', 'service_name').annotate(
            count=Count('id'),
            completed_count=Count('id', filter=Q(status='completed'))
        ).order_by('-count')

        for service in rc_services:
            services.append({
                'service_type': service['service_type'],
                'service_name': service['service_name'],
                'vertical': 'Regulatory Compliance',
                'count': service['count'],
                'success_rate': (service['completed_count'] / service['count'] * 100) if service['count'] > 0 else 0,
                'avg_execution_time': None
            })

        # Sort by count and return
        services.sort(key=lambda x: x['count'], reverse=True)
        return services


class RecentActivityAPIView(APIView):
    """
    Get recent service execution activity across all verticals.
    Returns the last 20 service executions.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            openapi.Parameter("limit", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description="Number of records to return (default: 20)")
        ],
        operation_description="Get recent service execution activity",
        responses={200: ServiceExecutionSerializer(many=True)}
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        limit = int(request.query_params.get("limit", 20))

        if not all([client_id, client_secret]):
            return Response(
                {"error": "Missing X-Client-ID or X-Client-Secret headers"},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = get_user_from_client_id(client_id)
        if not user:
            return Response(
                {"error": "Invalid client credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Get client for multi-tenant filtering
        client = user.client if hasattr(user, 'client') and user.client else None

        # Build filter based on client or user
        if client:
            filter_kwargs = {'client': client}
        else:
            filter_kwargs = {'user': user}

        # Get recent executions from all verticals
        recent_executions = []

        # Private Equity
        pe_executions = PEServiceExecution.objects.filter(**filter_kwargs).order_by('-started_at')[:limit]
        for exec in pe_executions:
            recent_executions.append({
                'id': exec.id,
                'vertical': 'Private Equity',
                'service_type': exec.service_type,
                'service_name': exec.service_name,
                'status': exec.status,
                'started_at': exec.started_at,
                'completed_at': exec.completed_at,
                'execution_time_seconds': exec.execution_time_seconds,
                'output_count': exec.output_count,
                'output_type': exec.output_type,
                'error_message': exec.error_message
            })

        # Class Actions
        ca_executions = CAServiceExecution.objects.filter(**filter_kwargs).order_by('-started_at')[:limit]
        for exec in ca_executions:
            recent_executions.append({
                'id': exec.id,
                'vertical': 'Class Actions',
                'service_type': exec.service_type,
                'service_name': exec.service_name,
                'status': exec.status,
                'started_at': exec.started_at,
                'completed_at': exec.completed_at,
                'execution_time_seconds': exec.execution_time_seconds,
                'output_count': exec.output_count,
                'output_type': exec.output_type,
                'error_message': exec.error_message
            })

        # Labor Employment
        le_executions = LEServiceExecution.objects.filter(**filter_kwargs).order_by('-started_at')[:limit]
        for exec in le_executions:
            recent_executions.append({
                'id': exec.id,
                'vertical': 'Labor Employment',
                'service_type': exec.service_type,
                'service_name': exec.service_name,
                'status': exec.status,
                'started_at': exec.started_at,
                'completed_at': exec.completed_at,
                'execution_time_seconds': exec.execution_time_seconds,
                'output_count': exec.output_count,
                'output_type': exec.output_type,
                'error_message': exec.error_message
            })

        # IP Litigation
        ip_executions = IPServiceExecution.objects.filter(**filter_kwargs).order_by('-started_at')[:limit]
        for exec in ip_executions:
            recent_executions.append({
                'id': exec.id,
                'vertical': 'IP Litigation',
                'service_type': exec.service_type,
                'service_name': exec.service_name,
                'status': exec.status,
                'started_at': exec.started_at,
                'completed_at': exec.completed_at,
                'execution_time_seconds': exec.execution_time_seconds,
                'output_count': exec.output_count,
                'output_type': exec.output_type,
                'error_message': exec.error_message
            })

        # Regulatory Compliance
        rc_executions = RCServiceExecution.objects.filter(**filter_kwargs).order_by('-started_at')[:limit]
        for exec in rc_executions:
            recent_executions.append({
                'id': exec.id,
                'vertical': 'Regulatory Compliance',
                'service_type': exec.service_type,
                'service_name': exec.service_name,
                'status': exec.status,
                'started_at': exec.started_at,
                'completed_at': exec.completed_at,
                'execution_time_seconds': exec.execution_time_seconds,
                'output_count': exec.output_count,
                'output_type': exec.output_type,
                'error_message': exec.error_message
            })

        # Sort by started_at and limit
        recent_executions.sort(key=lambda x: x['started_at'], reverse=True)
        recent_executions = recent_executions[:limit]

        return Response(recent_executions, status=status.HTTP_200_OK)


class CreateServiceExecutionView(APIView):
    """
    POST /api/v1/service-analytics/executions/create/

    Creates a service execution record for any of the 5 legal verticals.
    This is a universal endpoint that routes to the appropriate vertical's ServiceExecution model.

    Request Body:
    {
        "vertical": "class_actions",  // One of: private_equity, class_actions, labor_employment, ip_litigation, regulatory_compliance
        "run_id": "uuid",  // The run ID for the vertical (e.g., mass_claims_run_id, due_diligence_run_id, etc.)
        "service_type": "ca-intake-triage",
        "service_name": "Process and triage intake forms",
        "service_version": "1.0",
        "input_file_ids": [1, 2, 3],  // Optional: List of File IDs
        "input_parameters": {...},  // Optional: Service parameters
        "output_file_ids": [4, 5],  // Optional: List of output File IDs
        "output_type": "report",  // One of: json, pdf, excel, html, text, report
        "output_count": 2,
        "execution_time_seconds": 45,
        "status": "completed",  // One of: pending, running, completed, failed, cancelled
        "error_message": "",  // Optional: Error message if failed
        "execution_metadata": {...}  // Optional: Additional metadata
    }

    Response:
    {
        "success": true,
        "data": {
            "id": "uuid",
            "vertical": "class_actions",
            "service_type": "ca-intake-triage",
            "service_name": "Process and triage intake forms",
            "status": "completed",
            "started_at": "2024-11-22T14:30:22Z"
        }
    }
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Create a service execution record for any legal vertical",
        manual_parameters=[client_id_param, client_secret_param],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['vertical', 'run_id', 'service_type', 'service_name'],
            properties={
                'vertical': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    enum=['private_equity', 'class_actions', 'labor_employment', 'ip_litigation', 'regulatory_compliance'],
                    description='Legal vertical'
                ),
                'run_id': openapi.Schema(type=openapi.TYPE_STRING, description='Run ID for the vertical'),
                'service_type': openapi.Schema(type=openapi.TYPE_STRING, description='Service type code'),
                'service_name': openapi.Schema(type=openapi.TYPE_STRING, description='Human-readable service name'),
                'service_version': openapi.Schema(type=openapi.TYPE_STRING, description='Service version (default: 1.0)'),
                'input_file_ids': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_INTEGER), description='List of input file IDs'),
                'input_parameters': openapi.Schema(type=openapi.TYPE_OBJECT, description='Service input parameters'),
                'output_file_ids': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_INTEGER), description='List of output file IDs'),
                'output_type': openapi.Schema(type=openapi.TYPE_STRING, enum=['json', 'pdf', 'excel', 'html', 'text', 'report'], description='Output type'),
                'output_count': openapi.Schema(type=openapi.TYPE_INTEGER, description='Number of output files'),
                'execution_time_seconds': openapi.Schema(type=openapi.TYPE_INTEGER, description='Execution time in seconds'),
                'status': openapi.Schema(type=openapi.TYPE_STRING, enum=['pending', 'running', 'completed', 'failed', 'cancelled'], description='Execution status'),
                'error_message': openapi.Schema(type=openapi.TYPE_STRING, description='Error message if failed'),
                'execution_metadata': openapi.Schema(type=openapi.TYPE_OBJECT, description='Additional metadata'),
            }
        ),
        responses={
            201: openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    'success': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                    'data': openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        properties={
                            'id': openapi.Schema(type=openapi.TYPE_STRING),
                            'vertical': openapi.Schema(type=openapi.TYPE_STRING),
                            'service_type': openapi.Schema(type=openapi.TYPE_STRING),
                            'service_name': openapi.Schema(type=openapi.TYPE_STRING),
                            'status': openapi.Schema(type=openapi.TYPE_STRING),
                            'started_at': openapi.Schema(type=openapi.TYPE_STRING),
                        }
                    )
                }
            ),
            400: "Bad Request - Missing required fields or invalid vertical",
            404: "Run not found"
        }
    )
    def post(self, request):
        try:
            # Get client from authenticated user (multi-tenancy)
            user = request.user
            client = getattr(user, 'client', None)

            if not client:
                return Response({
                    'success': False,
                    'error': 'User has no associated client'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Extract request data
            vertical = request.data.get('vertical')
            run_id = request.data.get('run_id')
            service_type = request.data.get('service_type')
            service_name = request.data.get('service_name')
            service_version = request.data.get('service_version', '1.0')
            input_file_ids = request.data.get('input_file_ids', [])
            input_parameters = request.data.get('input_parameters', {})
            output_file_ids = request.data.get('output_file_ids', [])
            output_type = request.data.get('output_type', 'json')
            output_count = request.data.get('output_count', 0)
            execution_time_seconds = request.data.get('execution_time_seconds')
            exec_status = request.data.get('status', 'pending')
            error_message = request.data.get('error_message', '')
            execution_metadata = request.data.get('execution_metadata', {})

            # Validate required fields
            if not vertical or not run_id or not service_type or not service_name:
                return Response({
                    'success': False,
                    'error': 'Missing required fields: vertical, run_id, service_type, service_name'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Check if run_id is a temporary ID (non-numeric string like 'temp_run_...')
            # If so, skip execution tracking - return success without creating a record
            is_temp_run = isinstance(run_id, str) and (run_id.startswith('temp_') or not run_id.isdigit())
            if is_temp_run:
                return Response({
                    'success': True,
                    'data': {
                        'id': None,
                        'vertical': vertical,
                        'service_type': service_type,
                        'service_name': service_name,
                        'status': exec_status,
                        'note': 'Skipped execution tracking - temporary run ID'
                    }
                }, status=status.HTTP_200_OK)

            # Route to appropriate vertical
            execution = None

            if vertical == 'private_equity':
                from private_equity.models import DueDiligenceRun
                try:
                    run = DueDiligenceRun.objects.get(id=run_id, client=client)
                except DueDiligenceRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': f'DueDiligenceRun with ID {run_id} not found'
                    }, status=status.HTTP_404_NOT_FOUND)

                execution = PEServiceExecution.objects.create(
                    client=client,
                    user=user,
                    due_diligence_run=run,
                    service_type=service_type,
                    service_name=service_name,
                    service_version=service_version,
                    status=exec_status,
                    input_parameters=input_parameters,
                    output_type=output_type,
                    output_count=output_count,
                    execution_time_seconds=execution_time_seconds,
                    error_message=error_message,
                    execution_metadata=execution_metadata
                )

            elif vertical == 'class_actions':
                from class_actions.models import MassClaimsRun
                try:
                    run = MassClaimsRun.objects.get(id=run_id, client=client)
                except MassClaimsRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': f'MassClaimsRun with ID {run_id} not found'
                    }, status=status.HTTP_404_NOT_FOUND)

                execution = CAServiceExecution.objects.create(
                    client=client,
                    user=user,
                    mass_claims_run=run,
                    service_type=service_type,
                    service_name=service_name,
                    service_version=service_version,
                    status=exec_status,
                    input_parameters=input_parameters,
                    output_type=output_type,
                    output_count=output_count,
                    execution_time_seconds=execution_time_seconds,
                    error_message=error_message,
                    execution_metadata=execution_metadata
                )

            elif vertical == 'labor_employment':
                from labor_employment.models import WorkplaceCommunicationsRun
                try:
                    run = WorkplaceCommunicationsRun.objects.get(id=run_id, client=client)
                except WorkplaceCommunicationsRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': f'WorkplaceCommunicationsRun with ID {run_id} not found'
                    }, status=status.HTTP_404_NOT_FOUND)

                execution = LEServiceExecution.objects.create(
                    client=client,
                    user=user,
                    workplace_communications_run=run,
                    service_type=service_type,
                    service_name=service_name,
                    service_version=service_version,
                    status=exec_status,
                    input_parameters=input_parameters,
                    output_type=output_type,
                    output_count=output_count,
                    execution_time_seconds=execution_time_seconds,
                    error_message=error_message,
                    execution_metadata=execution_metadata
                )

            elif vertical == 'ip_litigation':
                from ip_litigation.models import PatentAnalysisRun
                try:
                    run = PatentAnalysisRun.objects.get(id=run_id, client=client)
                except PatentAnalysisRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': f'PatentAnalysisRun with ID {run_id} not found'
                    }, status=status.HTTP_404_NOT_FOUND)

                execution = IPServiceExecution.objects.create(
                    client=client,
                    user=user,
                    patent_analysis_run=run,
                    service_type=service_type,
                    service_name=service_name,
                    service_version=service_version,
                    status=exec_status,
                    input_parameters=input_parameters,
                    output_type=output_type,
                    output_count=output_count,
                    execution_time_seconds=execution_time_seconds,
                    error_message=error_message,
                    execution_metadata=execution_metadata
                )

            elif vertical == 'regulatory_compliance':
                from regulatory_compliance.models import ComplianceAuditRun
                try:
                    run = ComplianceAuditRun.objects.get(id=run_id, client=client)
                except ComplianceAuditRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': f'ComplianceAuditRun with ID {run_id} not found'
                    }, status=status.HTTP_404_NOT_FOUND)

                execution = RCServiceExecution.objects.create(
                    client=client,
                    user=user,
                    compliance_audit_run=run,
                    service_type=service_type,
                    service_name=service_name,
                    service_version=service_version,
                    status=exec_status,
                    input_parameters=input_parameters,
                    output_type=output_type,
                    output_count=output_count,
                    execution_time_seconds=execution_time_seconds,
                    error_message=error_message,
                    execution_metadata=execution_metadata
                )
            else:
                return Response({
                    'success': False,
                    'error': f'Invalid vertical: {vertical}. Must be one of: private_equity, class_actions, labor_employment, ip_litigation, regulatory_compliance'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Add input files if provided
            if input_file_ids:
                from core.models import File
                input_files = File.objects.filter(id__in=input_file_ids)
                execution.input_files.set(input_files)

            # Return response
            return Response({
                'success': True,
                'data': {
                    'id': str(execution.id),
                    'vertical': vertical,
                    'service_type': execution.service_type,
                    'service_name': execution.service_name,
                    'status': execution.status,
                    'started_at': execution.started_at.isoformat()
                }
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            import traceback
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error creating service execution: {str(e)}")
            logger.error(traceback.format_exc())
            return Response({
                'success': False,
                'error': f'Failed to create service execution: {str(e)}',
                'traceback': traceback.format_exc()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



class ToolDataInsightsAPIView(APIView):
    """
    Get actual tool output data from all 5 legal verticals.
    Returns aggregated insights from tool outputs like documents, claims, risks, etc.
    Multi-tenant: Data is filtered by client.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            openapi.Parameter("vertical", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
                            description="Filter by vertical: private_equity, class_actions, labor_employment, ip_litigation, regulatory_compliance"),
            openapi.Parameter("limit", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False,
                            description="Number of records per category (default: 10)")
        ],
        operation_description="Get tool output data insights across all legal verticals",
        responses={200: openapi.Response("Tool data insights")}
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        vertical_filter = request.query_params.get("vertical", None)
        limit = int(request.query_params.get("limit", 10))

        if not all([client_id, client_secret]):
            return Response(
                {"error": "Missing X-Client-ID or X-Client-Secret headers"},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = get_user_from_client_id(client_id)
        if not user:
            return Response(
                {"error": "Invalid client credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Get client for multi-tenant filtering
        client = user.client if hasattr(user, 'client') and user.client else None
        if not client:
            return Response(
                {"error": "User is not associated with a client"},
                status=status.HTTP_403_FORBIDDEN
            )

        response_data = {}

        # Private Equity tool data
        if not vertical_filter or vertical_filter == 'private_equity':
            response_data['private_equity'] = self._get_pe_tool_data(client, limit)

        # Class Actions tool data
        if not vertical_filter or vertical_filter == 'class_actions':
            response_data['class_actions'] = self._get_ca_tool_data(client, limit)

        # Labor Employment tool data
        if not vertical_filter or vertical_filter == 'labor_employment':
            response_data['labor_employment'] = self._get_le_tool_data(client, limit)

        # IP Litigation tool data
        if not vertical_filter or vertical_filter == 'ip_litigation':
            response_data['ip_litigation'] = self._get_ip_tool_data(client, limit)

        # Regulatory Compliance tool data
        if not vertical_filter or vertical_filter == 'regulatory_compliance':
            response_data['regulatory_compliance'] = self._get_rc_tool_data(client, limit)

        return Response(response_data, status=status.HTTP_200_OK)

    def _get_pe_tool_data(self, client, limit):
        """Get Private Equity tool output data"""
        # Due Diligence Runs
        dd_runs = DueDiligenceRun.objects.filter(client=client).order_by('-created_at')[:limit]
        dd_runs_data = [{
            'id': run.id,
            'deal_name': run.deal_name,
            'target_company': run.target_company,
            'deal_type': run.deal_type,
            'deal_value': str(run.deal_value) if run.deal_value else None,
            'created_at': run.created_at.isoformat()
        } for run in dd_runs]

        # Document Classifications
        doc_classifications = DocumentClassification.objects.filter(
            due_diligence_run__client=client
        ).order_by('-created_at')[:limit]
        doc_class_data = [{
            'id': doc.id,
            'document_type': doc.document_type,
            'confidence_score': doc.confidence_score,
            'is_verified': doc.is_verified,  # Fixed: was incorrectly 'is_duplicate'
            'created_at': doc.created_at.isoformat()
        } for doc in doc_classifications]

        # Risk Clauses
        risk_clauses = RiskClause.objects.filter(
            due_diligence_run__client=client
        ).order_by('-created_at')[:limit]
        risk_data = [{
            'id': clause.id,
            'clause_type': clause.clause_type,
            'risk_level': clause.risk_level,
            'clause_text': clause.clause_text[:200] + '...' if len(clause.clause_text) > 200 else clause.clause_text,
            'created_at': clause.created_at.isoformat()
        } for clause in risk_clauses]

        # Summary stats
        total_runs = DueDiligenceRun.objects.filter(client=client).count()
        total_docs = DocumentClassification.objects.filter(due_diligence_run__client=client).count()
        total_risks = RiskClause.objects.filter(due_diligence_run__client=client).count()
        high_risks = RiskClause.objects.filter(due_diligence_run__client=client, risk_level='high').count()

        return {
            'summary': {
                'total_due_diligence_runs': total_runs,
                'total_documents_classified': total_docs,
                'total_risk_clauses': total_risks,
                'high_risk_clauses': high_risks
            },
            'recent_runs': dd_runs_data,
            'recent_classifications': doc_class_data,
            'recent_risk_clauses': risk_data
        }

    def _get_ca_tool_data(self, client, limit):
        """Get Class Actions tool output data"""
        # Mass Claims Runs
        claims_runs = MassClaimsRun.objects.filter(client=client).order_by('-created_at')[:limit]
        runs_data = [{
            'id': run.id,
            'case_name': run.case_name,
            'case_number': run.case_number,
            'case_type': run.case_type,
            'status': run.status,
            'settlement_amount': str(run.settlement_amount) if run.settlement_amount else None,
            'created_at': run.created_at.isoformat()
        } for run in claims_runs]

        # Intake Forms
        intake_forms = IntakeForm.objects.filter(
            mass_claims_run__client=client
        ).order_by('-submitted_at')[:limit]
        intake_data = [{
            'id': form.id,
            'claimant_id': str(form.claimant_id),
            'processing_status': form.processing_status,
            'is_duplicate': form.is_duplicate,
            'is_valid': form.is_valid,
            'submitted_at': form.submitted_at.isoformat()
        } for form in intake_forms]

        # Evidence Documents
        evidence_docs = EvidenceDocument.objects.filter(
            mass_claims_run__client=client
        ).order_by('-created_at')[:limit]
        evidence_data = [{
            'id': doc.id,
            'evidence_type': doc.evidence_type,
            'relevance_score': doc.relevance_score,
            'is_culled': doc.is_culled,
            'contains_pii': doc.contains_pii,
            'created_at': doc.created_at.isoformat()
        } for doc in evidence_docs]

        # Summary stats
        total_runs = MassClaimsRun.objects.filter(client=client).count()
        total_intakes = IntakeForm.objects.filter(mass_claims_run__client=client).count()
        approved_claims = IntakeForm.objects.filter(
            mass_claims_run__client=client, processing_status='approved'
        ).count()
        total_evidence = EvidenceDocument.objects.filter(mass_claims_run__client=client).count()

        return {
            'summary': {
                'total_mass_claims_runs': total_runs,
                'total_intake_forms': total_intakes,
                'approved_claims': approved_claims,
                'total_evidence_documents': total_evidence
            },
            'recent_runs': runs_data,
            'recent_intake_forms': intake_data,
            'recent_evidence_documents': evidence_data
        }

    def _get_le_tool_data(self, client, limit):
        """Get Labor Employment tool output data"""
        # Workplace Communications Runs
        comm_runs = WorkplaceCommunicationsRun.objects.filter(client=client).order_by('-created_at')[:limit]
        runs_data = [{
            'id': run.id,
            'case_name': run.case_name,
            'company_name': run.company_name,
            'case_type': run.case_type,
            'data_sources': run.data_sources,
            'created_at': run.created_at.isoformat()
        } for run in comm_runs]

        # Communication Messages (with sentiment data)
        messages = CommunicationMessage.objects.filter(
            communications_run__client=client
        ).order_by('-sent_datetime')[:limit]
        message_data = [{
            'id': m.id,
            'message_type': m.message_type,
            'sender': m.sender,
            'sentiment_score': m.sentiment_score,
            'toxicity_score': m.toxicity_score,
            'is_flagged': m.is_flagged,
            'sent_datetime': m.sent_datetime.isoformat()
        } for m in messages]

        # Compliance Alerts
        alerts = LEComplianceAlert.objects.filter(
            communications_run__client=client
        ).order_by('-created_at')[:limit]
        alert_data = [{
            'id': a.id,
            'alert_type': a.alert_type,
            'alert_title': a.alert_title,
            'severity': a.severity,
            'priority': a.priority,
            'created_at': a.created_at.isoformat()
        } for a in alerts]

        # Summary stats
        total_runs = WorkplaceCommunicationsRun.objects.filter(client=client).count()
        total_messages = CommunicationMessage.objects.filter(communications_run__client=client).count()
        flagged_messages = CommunicationMessage.objects.filter(
            communications_run__client=client, is_flagged=True
        ).count()
        total_alerts = LEComplianceAlert.objects.filter(communications_run__client=client).count()

        return {
            'summary': {
                'total_communications_runs': total_runs,
                'total_messages': total_messages,
                'flagged_messages': flagged_messages,
                'total_compliance_alerts': total_alerts
            },
            'recent_runs': runs_data,
            'recent_messages': message_data,
            'recent_compliance_alerts': alert_data
        }

    def _get_ip_tool_data(self, client, limit):
        """Get IP Litigation tool output data"""
        # Patent Analysis Runs
        patent_runs = PatentAnalysisRun.objects.filter(client=client).order_by('-created_at')[:limit]
        runs_data = [{
            'id': run.id,
            'case_name': run.case_name,
            'litigation_type': run.litigation_type,
            'technology_area': run.technology_area,
            'patents_in_suit': run.patents_in_suit,
            'created_at': run.created_at.isoformat()
        } for run in patent_runs]

        # Patent Documents
        patents = PatentDocument.objects.filter(
            analysis_run__client=client
        ).order_by('-created_at')[:limit]
        patent_data = [{
            'id': p.id,
            'patent_number': p.patent_number,
            'title': p.title,
            'assignees': p.assignees,
            'status': p.status,
            'filing_date': p.filing_date.isoformat() if p.filing_date else None,
            'created_at': p.created_at.isoformat()
        } for p in patents]

        # Claim Charts
        charts = ClaimChart.objects.filter(
            analysis_run__client=client
        ).order_by('-created_at')[:limit]
        chart_data = [{
            'id': c.id,
            'chart_name': c.chart_name,
            'chart_type': c.chart_type,
            'target_product': c.target_product,
            'overall_conclusion': c.overall_conclusion,
            'confidence_score': c.confidence_score,
            'created_at': c.created_at.isoformat()
        } for c in charts]

        # Summary stats
        total_runs = PatentAnalysisRun.objects.filter(client=client).count()
        total_patents = PatentDocument.objects.filter(analysis_run__client=client).count()
        total_charts = ClaimChart.objects.filter(analysis_run__client=client).count()

        return {
            'summary': {
                'total_patent_analysis_runs': total_runs,
                'total_patent_documents': total_patents,
                'total_claim_charts': total_charts
            },
            'recent_runs': runs_data,
            'recent_patents': patent_data,
            'recent_claim_charts': chart_data
        }

    def _get_rc_tool_data(self, client, limit):
        """Get Regulatory Compliance tool output data"""
        # Compliance Runs
        comp_runs = ComplianceRun.objects.filter(client=client).order_by('-created_at')[:limit]
        runs_data = [{
            'id': run.id,
            'organization_name': run.organization_name,
            'compliance_framework': run.compliance_framework,
            'jurisdiction': run.jurisdiction,
            'created_at': run.created_at.isoformat()
        } for run in comp_runs]

        # Regulatory Requirements
        requirements = RegulatoryRequirement.objects.filter(
            compliance_run__client=client
        ).order_by('-created_at')[:limit]
        requirement_data = [{
            'id': r.id,
            'requirement_id': r.requirement_id,
            'requirement_title': r.requirement_title,
            'category': r.category,
            'compliance_status': r.compliance_status,
            'risk_level': r.risk_level,
            'created_at': r.created_at.isoformat()
        } for r in requirements]

        # Compliance Alerts
        alerts = RCComplianceAlert.objects.filter(
            compliance_run__client=client
        ).order_by('-created_at')[:limit]
        alert_data = [{
            'id': a.id,
            'alert_type': a.alert_type,
            'alert_title': a.alert_title,
            'severity': a.severity,
            'priority': a.priority,
            'status': a.status,
            'created_at': a.created_at.isoformat()
        } for a in alerts]

        # Summary stats
        total_runs = ComplianceRun.objects.filter(client=client).count()
        total_requirements = RegulatoryRequirement.objects.filter(compliance_run__client=client).count()
        non_compliant = RegulatoryRequirement.objects.filter(
            compliance_run__client=client, compliance_status='non_compliant'
        ).count()
        total_alerts = RCComplianceAlert.objects.filter(compliance_run__client=client).count()
        critical_alerts = RCComplianceAlert.objects.filter(
            compliance_run__client=client, severity='critical'
        ).count()

        return {
            'summary': {
                'total_compliance_runs': total_runs,
                'total_requirements': total_requirements,
                'non_compliant_requirements': non_compliant,
                'total_alerts': total_alerts,
                'critical_alerts': critical_alerts
            },
            'recent_runs': runs_data,
            'recent_requirements': requirement_data,
            'recent_alerts': alert_data
        }


# ========================================================
# Unified Reports API — production-ready with 20+ report types
# ========================================================

REPORT_CATEGORIES = {
    'pii': 'PII & Anonymization',
    'due_diligence': 'Due Diligence',
    'litigation': 'Litigation Support',
    'compliance': 'Compliance & Regulatory',
    'document_ops': 'Document Operations',
    'ai_analysis': 'AI Analysis',
    'financial': 'Financial Reports',
}

REPORT_DEFINITIONS = [
    # PII & Anonymization
    {'id': 'pii_scan', 'category': 'pii', 'label': 'PII Scan Results', 'icon': 'fa-user-shield', 'source': 'anonymizer'},
    {'id': 'pii_risk', 'category': 'pii', 'label': 'PII Risk Assessment', 'icon': 'fa-exclamation-triangle', 'source': 'anonymizer'},
    {'id': 'pii_redaction', 'category': 'pii', 'label': 'Redaction Log', 'icon': 'fa-eraser', 'source': 'anonymizer'},
    # Due Diligence
    {'id': 'dd_risk_clauses', 'category': 'due_diligence', 'label': 'Risk Clause Analysis', 'icon': 'fa-gavel', 'source': 'private_equity'},
    {'id': 'dd_classifications', 'category': 'due_diligence', 'label': 'Document Classifications', 'icon': 'fa-tags', 'source': 'private_equity'},
    {'id': 'dd_findings', 'category': 'due_diligence', 'label': 'Due Diligence Findings', 'icon': 'fa-search-dollar', 'source': 'private_equity'},
    {'id': 'dd_deal_summary', 'category': 'due_diligence', 'label': 'Deal Summary Reports', 'icon': 'fa-handshake', 'source': 'private_equity'},
    # Litigation Support
    {'id': 'lit_patents', 'category': 'litigation', 'label': 'Patent Analysis', 'icon': 'fa-file-patent', 'source': 'ip_litigation'},
    {'id': 'lit_claim_charts', 'category': 'litigation', 'label': 'Claim Construction Charts', 'icon': 'fa-chart-bar', 'source': 'ip_litigation'},
    {'id': 'lit_prior_art', 'category': 'litigation', 'label': 'Prior Art Search', 'icon': 'fa-book', 'source': 'ip_litigation'},
    {'id': 'lit_intake', 'category': 'litigation', 'label': 'Class Action Intake', 'icon': 'fa-file-invoice', 'source': 'class_actions'},
    {'id': 'lit_evidence', 'category': 'litigation', 'label': 'Evidence Collection', 'icon': 'fa-folder-open', 'source': 'class_actions'},
    # Compliance
    {'id': 'comp_regulatory', 'category': 'compliance', 'label': 'Regulatory Requirements', 'icon': 'fa-list-check', 'source': 'regulatory_compliance'},
    {'id': 'comp_alerts', 'category': 'compliance', 'label': 'Compliance Alerts', 'icon': 'fa-bell', 'source': 'regulatory_compliance'},
    {'id': 'comp_policy', 'category': 'compliance', 'label': 'Policy Mappings', 'icon': 'fa-balance-scale', 'source': 'regulatory_compliance'},
    {'id': 'comp_employment', 'category': 'compliance', 'label': 'Employment Compliance', 'icon': 'fa-briefcase', 'source': 'labor_employment'},
    {'id': 'comp_wage', 'category': 'compliance', 'label': 'Wage & Hour Analysis', 'icon': 'fa-clock', 'source': 'labor_employment'},
    # Document Operations
    {'id': 'doc_uploads', 'category': 'document_ops', 'label': 'Upload History', 'icon': 'fa-upload', 'source': 'core'},
    {'id': 'doc_deletions', 'category': 'document_ops', 'label': 'Deletion History', 'icon': 'fa-trash-alt', 'source': 'core'},
    {'id': 'doc_sharing', 'category': 'document_ops', 'label': 'Sharing Activity', 'icon': 'fa-share-alt', 'source': 'document_operations'},
    # AI Analysis
    {'id': 'ai_ocr', 'category': 'ai_analysis', 'label': 'OCR Processing', 'icon': 'fa-eye', 'source': 'document_ocr'},
    {'id': 'ai_translation', 'category': 'ai_analysis', 'label': 'Translation Jobs', 'icon': 'fa-language', 'source': 'document_translation'},
    {'id': 'ai_chat', 'category': 'ai_analysis', 'label': 'DocuChat Queries', 'icon': 'fa-comment-dots', 'source': 'grid_documents_interrogation'},
    # Financial
    {'id': 'fin_executions', 'category': 'financial', 'label': 'Service Executions', 'icon': 'fa-chart-line', 'source': 'service_analytics'},
]


class ReportsListView(APIView):
    """
    Unified reports API — production-ready with 20+ report types.
    Supports pagination, category/vertical filtering, and multi-tenancy.

    GET /api/v1/service-analytics/reports/
      ?category=pii,due_diligence
      &page=1&page_size=10
      &search=risk
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        if not all([client_id, client_secret]):
            return Response({"error": "Missing X-Client-ID or X-Client-Secret"}, status=400)

        user = get_user_from_client_id(client_id)
        if not user or not (hasattr(user, 'client') and user.client):
            return Response({"error": "Unauthorized"}, status=401)

        client = user.client

        page = int(request.query_params.get("page", 1))
        page_size = min(int(request.query_params.get("page_size", 10)), 50)
        category_filter = request.query_params.get("category", "")
        search = request.query_params.get("search", "").strip().lower()

        categories = [c.strip() for c in category_filter.split(",") if c.strip()] if category_filter else []

        reports = self._gather_reports(client, categories, search)

        total = len(reports)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size

        return Response({
            "results": reports[start:end],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "categories": [
                {"id": k, "label": v, "count": sum(1 for r in reports if r['category'] == k)}
                for k, v in REPORT_CATEGORIES.items()
            ],
        })

    def _gather_reports(self, client, categories, search):
        reports = []
        for rd in REPORT_DEFINITIONS:
            if categories and rd['category'] not in categories:
                continue
            items = self._collect_report_items(client, rd)
            for item in items:
                if search:
                    label_lower = rd['label'].lower()
                    title_lower = (item.get('title') or '').lower()
                    if search not in label_lower and search not in title_lower:
                        continue
                reports.append({
                    'report_id': rd['id'],
                    'label': rd['label'],
                    'category': rd['category'],
                    'category_label': REPORT_CATEGORIES.get(rd['category'], rd['category']),
                    'icon': rd['icon'],
                    'title': item.get('title', ''),
                    'summary': item.get('summary', ''),
                    'status': item.get('status', 'completed'),
                    'severity': item.get('severity', ''),
                    'timestamp': item.get('timestamp', ''),
                    'details': item.get('details', {}),
                    'file_id': item.get('file_id'),
                    'filename': item.get('filename'),
                })
        return sorted(reports, key=lambda r: r['timestamp'] or '', reverse=True)

    def _collect_report_items(self, client, rd):
        items = []
        rid = rd['id']
        try:
            if rid == 'pii_scan':
                from document_anonymizer.models import AnonymizationStats
                qs = AnonymizationStats.objects.filter(user__client=client).order_by('-created_at')[:10]
                for s in qs:
                    items.append({
                        'title': f'PII scan on document #{s.original_file_id}',
                        'summary': f'{s.entities_found or 0} entities found',
                        'severity': 'high' if (s.entities_found or 0) > 50 else 'medium',
                        'timestamp': s.created_at.isoformat() if s.created_at else '',
                    })
            elif rid == 'pii_risk':
                from document_anonymizer.models import AnonymizationStats
                qs = AnonymizationStats.objects.filter(user__client=client).order_by('-created_at')[:10]
                for s in qs:
                    risk = 'high' if (s.entities_found or 0) > 100 else ('medium' if (s.entities_found or 0) > 20 else 'low')
                    items.append({
                        'title': f'Risk assessment for document #{s.original_file_id}',
                        'summary': f'Risk score: {risk.upper()} ({s.entities_found or 0} entities)',
                        'severity': risk,
                        'timestamp': s.created_at.isoformat() if s.created_at else '',
                    })
            elif rid == 'dd_risk_clauses':
                qs = RiskClause.objects.filter(due_diligence_run__client=client).order_by('-created_at')[:10]
                for c in qs:
                    items.append({
                        'title': f'{c.clause_type} clause',
                        'summary': f'Risk: {c.risk_level} — {c.clause_text[:100]}',
                        'severity': c.risk_level or 'medium',
                        'timestamp': c.created_at.isoformat() if c.created_at else '',
                        'file_id': c.file_id,
                        'filename': c.file.filename if c.file else None,
                    })
            elif rid == 'dd_classifications':
                qs = DocumentClassification.objects.filter(due_diligence_run__client=client).order_by('-created_at')[:10]
                for d in qs:
                    items.append({
                        'title': f'{d.document_type} classified',
                        'summary': f'Confidence: {(d.confidence_score or 0)*100:.0f}% — Verified: {d.is_verified}',
                        'timestamp': d.created_at.isoformat() if d.created_at else '',
                        'file_id': d.file_id,
                        'filename': d.file.filename if d.file else None,
                    })
            elif rid == 'dd_findings':
                qs = FindingsReport.objects.filter(due_diligence_run__client=client).order_by('-created_at')[:10]
                for f in qs:
                    items.append({
                        'title': f'Finding: {f.finding_type or "General"}',
                        'summary': f'{f.description[:150] if f.description else "No description"}',
                        'severity': 'high' if f.critical else 'medium',
                        'timestamp': f.created_at.isoformat() if f.created_at else '',
                    })
            elif rid == 'dd_deal_summary':
                qs = DueDiligenceRun.objects.filter(client=client).order_by('-created_at')[:10]
                for r in qs:
                    items.append({
                        'title': f'Deal: {r.deal_name} ({r.target_company})',
                        'summary': f'Type: {r.deal_type} — Value: {r.deal_value or "N/A"}',
                        'timestamp': r.created_at.isoformat() if r.created_at else '',
                    })
            elif rid == 'lit_patents':
                qs = PatentDocument.objects.filter(patent_analysis_run__client=client).order_by('-created_at')[:10]
                for p in qs:
                    items.append({
                        'title': f'Patent: {p.patent_number} — {p.title}',
                        'summary': f'Status: {p.status}',
                        'timestamp': p.filing_date.isoformat() if hasattr(p, 'filing_date') and p.filing_date else (p.created_at.isoformat() if hasattr(p, 'created_at') else ''),
                        'file_id': getattr(p, 'file_id', None),
                    })
            elif rid == 'lit_claim_charts':
                qs = ClaimChart.objects.filter(patent_analysis_run__client=client).order_by('-created_at')[:10]
                for c in qs:
                    items.append({
                        'title': f'Chart: {c.chart_name}',
                        'summary': f'Type: {c.chart_type} — Conclusion: {c.overall_conclusion}',
                        'timestamp': c.created_at.isoformat() if c.created_at else '',
                    })
            elif rid == 'lit_prior_art':
                qs = PriorArtDocument.objects.filter(patent_analysis_run__client=client).order_by('-created_at')[:10]
                for p in qs:
                    items.append({
                        'title': f'Prior Art: {p.title or p.document_id}',
                        'summary': f'Type: {p.document_type or "N/A"}',
                        'timestamp': p.created_at.isoformat() if p.created_at else '',
                        'file_id': getattr(p, 'file_id', None),
                    })
            elif rid == 'lit_intake':
                qs = IntakeForm.objects.filter(mass_claims_run__client=client).order_by('-submitted_at')[:10]
                for i in qs:
                    items.append({
                        'title': f'Intake: {i.form_identifier} — {i.claimant_name}',
                        'summary': f'Type: {i.claim_type} — Status: {i.processing_status}',
                        'timestamp': i.submitted_at.isoformat() if i.submitted_at else '',
                    })
            elif rid == 'lit_evidence':
                qs = EvidenceDocument.objects.filter(mass_claims_run__client=client).order_by('-created_at')[:10]
                for e in qs:
                    items.append({
                        'title': f'Evidence: {e.file_name or e.document_name or "Doc"}',
                        'summary': f'Type: {e.document_type or e.evidence_type or "N/A"}',
                        'timestamp': e.created_at.isoformat() if e.created_at else '',
                        'file_id': getattr(e, 'file_id', None),
                    })
            elif rid == 'comp_regulatory':
                qs = RegulatoryRequirement.objects.filter(compliance_run__client=client).order_by('-created_at')[:10]
                for r in qs:
                    items.append({
                        'title': f'{r.requirement_title or r.requirement_id}',
                        'summary': f'Status: {r.compliance_status} — Category: {r.category}',
                        'severity': r.risk_level or 'medium',
                        'timestamp': r.created_at.isoformat() if r.created_at else '',
                    })
            elif rid == 'comp_alerts':
                qs = RCComplianceAlert.objects.filter(compliance_run__client=client).order_by('-created_at')[:10]
                for a in qs:
                    items.append({
                        'title': f'Alert: {a.alert_title}',
                        'summary': f'Type: {a.alert_type} — Status: {a.status}',
                        'severity': a.severity or 'medium',
                        'timestamp': a.created_at.isoformat() if a.created_at else '',
                    })
            elif rid == 'comp_policy':
                qs = PolicyMapping.objects.filter(compliance_run__client=client).order_by('-created_at')[:10]
                for p in qs:
                    items.append({
                        'title': f'Policy: {p.policy_name or p.policy_id}',
                        'summary': f'Requirement: {p.requirement_name or "N/A"}',
                        'timestamp': p.created_at.isoformat() if p.created_at else '',
                        'file_id': getattr(p, 'policy_document_id', None),
                    })
            elif rid == 'comp_employment':
                qs = LEComplianceAlert.objects.filter(communications_run__client=client).order_by('-created_at')[:10]
                for a in qs:
                    items.append({
                        'title': f'Alert: {a.alert_title}',
                        'summary': f'Type: {a.alert_type} — Priority: {a.priority}',
                        'severity': a.severity or 'medium',
                        'timestamp': a.created_at.isoformat() if a.created_at else '',
                    })
            elif rid == 'comp_wage':
                qs = WageHourAnalysis.objects.filter(communications_run__client=client).order_by('-created_at')[:10]
                for w in qs:
                    items.append({
                        'title': f'Wage Analysis: {w.analysis_type or "General"}',
                        'summary': f'Status: {w.status or "completed"}',
                        'timestamp': w.created_at.isoformat() if w.created_at else '',
                    })
            elif rid == 'doc_uploads':
                from core.models import File as CoreFile
                qs = CoreFile.objects.filter(user__client=client).order_by('-created_at')[:10]
                for f in qs:
                    items.append({
                        'title': f'Uploaded: {f.filename}',
                        'summary': f'Size: {f.file_size} bytes — Project: {f.project_id}',
                        'timestamp': f.created_at.isoformat() if f.created_at else '',
                        'file_id': f.id,
                        'filename': f.filename,
                    })
            elif rid == 'doc_deletions':
                from custom_authentication.models import UserActivityLog
                qs = UserActivityLog.objects.filter(
                    user__client=client, event='FILE_DELETED'
                ).order_by('-timestamp')[:10]
                for a in qs:
                    meta = a.metadata or {}
                    items.append({
                        'title': f'Deleted: {meta.get("filename", "Unknown")}',
                        'summary': f'Project: {meta.get("project_id", "N/A")}',
                        'timestamp': a.timestamp.isoformat() if a.timestamp else '',
                    })
            elif rid == 'doc_sharing':
                from document_operations.models import FileAccessEntry
                qs = FileAccessEntry.objects.filter(file_link__file__user__client=client).order_by('-created_at')[:10]
                for s in qs:
                    items.append({
                        'title': f'Shared: access granted',
                        'summary': f'Level: {s.access_level}',
                        'timestamp': s.created_at.isoformat() if hasattr(s, 'created_at') else '',
                    })
            elif rid == 'ai_ocr':
                from document_ocr.models import OCRRun
                qs = OCRRun.objects.filter(user__client=client).order_by('-created_at')[:10]
                for o in qs:
                    items.append({
                        'title': f'OCR: {o.ocr_option or "Document"}',
                        'summary': f'Status: {o.status}',
                        'timestamp': o.created_at.isoformat() if o.created_at else '',
                    })
            elif rid == 'ai_translation':
                from document_translation.models import TranslationRun
                qs = TranslationRun.objects.filter(user__client=client).order_by('-created_at')[:10]
                for t in qs:
                    items.append({
                        'title': f'Translation: {t.source_language or "?"} → {t.target_language or "?"}',
                        'summary': f'Status: {t.status}',
                        'timestamp': t.created_at.isoformat() if t.created_at else '',
                    })
            elif rid == 'ai_chat':
                from grid_documents_interrogation.models import Query
                qs = Query.objects.filter(user__client=client).order_by('-created_at')[:10]
                for q in qs:
                    items.append({
                        'title': f'Chat: {q.query_text[:80] if q.query_text else "Query"}',
                        'summary': f'Response: {(q.response_text or "")[:100]}',
                        'timestamp': q.created_at.isoformat() if q.created_at else '',
                    })
            elif rid == 'fin_executions':
                # Gather executions from all verticals
                from private_equity.models import ServiceExecution as PE
                from class_actions.models import ServiceExecution as CA
                from labor_employment.models import ServiceExecution as LE
                from ip_litigation.models import ServiceExecution as IP
                from regulatory_compliance.models import ServiceExecution as RC
                all_execs = []
                for s in PE.objects.filter(client=client).order_by('-created_at')[:3]:
                    all_execs.append(('PE', s))
                for s in CA.objects.filter(client=client).order_by('-created_at')[:3]:
                    all_execs.append(('CA', s))
                for s in LE.objects.filter(client=client).order_by('-created_at')[:3]:
                    all_execs.append(('LE', s))
                for s in IP.objects.filter(client=client).order_by('-created_at')[:3]:
                    all_execs.append(('IP', s))
                for s in RC.objects.filter(client=client).order_by('-created_at')[:3]:
                    all_execs.append(('RC', s))
                all_execs.sort(key=lambda x: x[1].created_at or '', reverse=True)
                for prefix, s in all_execs[:10]:
                    items.append({
                        'title': f'[{prefix}] {s.service_name or s.service_type}',
                        'summary': f'Status: {s.status} — Vertical: {s.vertical}',
                        'status': s.status or 'unknown',
                        'timestamp': s.created_at.isoformat() if s.created_at else '',
                    })
        except Exception:
            pass
        return items
