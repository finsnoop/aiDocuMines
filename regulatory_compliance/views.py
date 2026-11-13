import time
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.generics import get_object_or_404
from django.db.models import Avg, Count, Q
from django.utils import timezone
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication

from core.utils import generate_and_register_service_report, clean_request_data_for_serializer, get_user_client

logger = logging.getLogger(__name__)

from custom_authentication.permissions import IsClientOrAdmin
from .models import (
    ComplianceRun, RegulatoryRequirement, PolicyMapping, DSARRequest,
    DataInventory, RedactionTask, ComplianceAlert,
    ServiceExecution, ServiceOutput
)
from .serializers import (
    ComplianceRunSerializer, ComplianceRunCreateSerializer,
    RegulatoryRequirementSerializer, PolicyMappingSerializer,
    DSARRequestSerializer, DataInventorySerializer, RedactionTaskSerializer,
    ComplianceAlertSerializer, ComplianceSummarySerializer, DSARSummarySerializer,
    RedactionSummarySerializer, AlertSummarySerializer
)
from .tasks import (
    analyze_regulatory_requirements_task, map_policies_to_requirements_task,
    process_dsar_request_task, perform_document_redaction_task,
    generate_compliance_report_task
)


class ComplianceRunListCreateView(APIView):
    """
    List and create compliance analysis runs.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get list of compliance runs",
        tags=["Regulatory Compliance - Analysis Runs"],
        responses={200: ComplianceRunSerializer(many=True)}
    )
    def get(self, request):
        """Get list of compliance runs for the user"""
        runs = ComplianceRun.objects.filter(run__user=request.user)
        serializer = ComplianceRunSerializer(runs, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Create a new compliance run",
        tags=["Regulatory Compliance - Analysis Runs"],
        request_body=ComplianceRunCreateSerializer,
        responses={201: ComplianceRunSerializer}
    )
    def post(self, request):
        """Create a new compliance run"""
        serializer = ComplianceRunCreateSerializer(data=clean_request_data_for_serializer(request.data, ComplianceRunCreateSerializer), context={'request': request})
        if serializer.is_valid():
            compliance_run = serializer.save()
            response_serializer = ComplianceRunSerializer(compliance_run)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ComplianceRunDetailView(APIView):
    """
    Retrieve, update, and delete compliance runs.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get compliance run details",
        tags=["Regulatory Compliance - Analysis Runs"],
        responses={200: ComplianceRunSerializer}
    )
    def get(self, request, pk):
        """Get compliance run details"""
        compliance_run = get_object_or_404(ComplianceRun, pk=pk, run__user=request.user)
        serializer = ComplianceRunSerializer(compliance_run)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Update compliance run",
        tags=["Regulatory Compliance - Analysis Runs"],
        request_body=ComplianceRunSerializer,
        responses={200: ComplianceRunSerializer}
    )
    def put(self, request, pk):
        """Update compliance run"""
        compliance_run = get_object_or_404(ComplianceRun, pk=pk, run__user=request.user)
        serializer = ComplianceRunSerializer(compliance_run, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @swagger_auto_schema(
        operation_description="Delete compliance run",
        tags=["Regulatory Compliance - Analysis Runs"],
        responses={204: "Compliance run deleted"}
    )
    def delete(self, request, pk):
        """Delete compliance run"""
        compliance_run = get_object_or_404(ComplianceRun, pk=pk, run__user=request.user)
        compliance_run.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RegulatoryRequirementView(APIView):
    """
    Manage regulatory requirements for compliance analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get regulatory requirements for a compliance run",
        tags=["Regulatory Compliance - Requirements"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('category', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('compliance_status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: RegulatoryRequirementSerializer(many=True)}
    )
    def get(self, request):
        """Get regulatory requirements for a compliance run"""
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)
        
        requirements = RegulatoryRequirement.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )
        
        # Apply filters
        category = request.query_params.get('category')
        if category:
            requirements = requirements.filter(category=category)
        
        compliance_status = request.query_params.get('compliance_status')
        if compliance_status:
            requirements = requirements.filter(compliance_status=compliance_status)
        
        serializer = RegulatoryRequirementSerializer(requirements, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Add a regulatory requirement",
        tags=["Regulatory Compliance - Requirements"],
        request_body=RegulatoryRequirementSerializer,
        responses={201: RegulatoryRequirementSerializer}
    )
    def post(self, request):
        """Add a regulatory requirement"""
        serializer = RegulatoryRequirementSerializer(data=clean_request_data_for_serializer(request.data, RegulatoryRequirementSerializer), context={'request': request})
        if serializer.is_valid():
            requirement = serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RequirementAnalysisView(APIView):
    """
    Trigger regulatory requirement analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Trigger regulatory requirement analysis",
        tags=["Regulatory Compliance - Requirements"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'compliance_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'framework_documents': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="List of File IDs containing regulatory framework documents"
                )
            },
            required=['compliance_run_id']
        ),
        responses={202: "Requirement analysis started"}
    )
    def post(self, request):
        """Trigger regulatory requirement analysis"""
        compliance_run_id = request.data.get('compliance_run_id')
        framework_documents = request.data.get('framework_documents', [])
        
        if not compliance_run_id:
            return Response(
                {"error": "compliance_run_id is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)
        
        # Trigger requirement analysis task
        task = analyze_regulatory_requirements_task.delay(
            compliance_run.id, framework_documents, request.user.id
        )
        
        return Response({
            "message": "Regulatory requirement analysis started",
            "task_id": task.id,
            "compliance_run_id": compliance_run_id
        }, status=status.HTTP_202_ACCEPTED)


class PolicyMappingView(APIView):
    """
    Manage policy mappings to regulatory requirements.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get policy mappings for a compliance run",
        tags=["Regulatory Compliance - Policy Mapping"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('mapping_strength', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: PolicyMappingSerializer(many=True)}
    )
    def get(self, request):
        """Get policy mappings for a compliance run"""
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)
        
        mappings = PolicyMapping.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )
        
        # Apply mapping strength filter
        mapping_strength = request.query_params.get('mapping_strength')
        if mapping_strength:
            mappings = mappings.filter(mapping_strength=mapping_strength)
        
        serializer = PolicyMappingSerializer(mappings, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Trigger policy mapping analysis",
        tags=["Regulatory Compliance - Policy Mapping"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'compliance_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'policy_documents': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="List of File IDs containing policy documents"
                )
            },
            required=['compliance_run_id', 'policy_documents']
        ),
        responses={202: "Policy mapping analysis started"}
    )
    def post(self, request):
        """Trigger policy mapping analysis"""
        compliance_run_id = request.data.get('compliance_run_id')
        policy_documents = request.data.get('policy_documents', [])
        
        if not compliance_run_id or not policy_documents:
            return Response(
                {"error": "compliance_run_id and policy_documents are required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)
        
        # Trigger policy mapping task
        task = map_policies_to_requirements_task.delay(
            compliance_run.id, policy_documents, request.user.id
        )
        
        return Response({
            "message": "Policy mapping analysis started",
            "task_id": task.id,
            "compliance_run_id": compliance_run_id,
            "policy_documents_count": len(policy_documents)
        }, status=status.HTTP_202_ACCEPTED)


class DSARRequestView(APIView):
    """
    Manage Data Subject Access Requests (DSAR).
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get DSAR requests for a compliance run",
        tags=["Regulatory Compliance - DSAR"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('request_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: DSARRequestSerializer(many=True)}
    )
    def get(self, request):
        """Get DSAR requests for a compliance run"""
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)
        
        requests = DSARRequest.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )
        
        # Apply filters
        request_status = request.query_params.get('status')
        if request_status:
            requests = requests.filter(status=request_status)
        
        request_type = request.query_params.get('request_type')
        if request_type:
            requests = requests.filter(request_type=request_type)
        
        serializer = DSARRequestSerializer(requests, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Create a new DSAR request",
        tags=["Regulatory Compliance - DSAR"],
        request_body=DSARRequestSerializer,
        responses={201: DSARRequestSerializer}
    )
    def post(self, request):
        """Create a new DSAR request"""
        serializer = DSARRequestSerializer(data=clean_request_data_for_serializer(request.data, DSARRequestSerializer), context={'request': request})
        if serializer.is_valid():
            dsar_request = serializer.save()
            
            # Trigger DSAR processing task
            process_dsar_request_task.delay(dsar_request.id, request.user.id)
            
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DSARRequestDetailView(APIView):
    """
    Retrieve, update, and delete DSAR requests.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get DSAR request details",
        tags=["Regulatory Compliance - DSAR"],
        responses={200: DSARRequestSerializer}
    )
    def get(self, request, pk):
        """Get DSAR request details"""
        dsar_request = get_object_or_404(DSARRequest, pk=pk, user=request.user)
        serializer = DSARRequestSerializer(dsar_request)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Update DSAR request",
        tags=["Regulatory Compliance - DSAR"],
        request_body=DSARRequestSerializer,
        responses={200: DSARRequestSerializer}
    )
    def put(self, request, pk):
        """Update DSAR request"""
        dsar_request = get_object_or_404(DSARRequest, pk=pk, user=request.user)
        serializer = DSARRequestSerializer(dsar_request, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RedactionTaskView(APIView):
    """
    Manage document redaction tasks.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get redaction tasks for a compliance run",
        tags=["Regulatory Compliance - Document Redaction"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('redaction_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: RedactionTaskSerializer(many=True)}
    )
    def get(self, request):
        """Get redaction tasks for a compliance run"""
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)

        tasks = RedactionTask.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )

        # Apply filters
        task_status = request.query_params.get('status')
        if task_status:
            tasks = tasks.filter(status=task_status)

        redaction_type = request.query_params.get('redaction_type')
        if redaction_type:
            tasks = tasks.filter(redaction_type=redaction_type)

        serializer = RedactionTaskSerializer(tasks, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new redaction task",
        tags=["Regulatory Compliance - Document Redaction"],
        request_body=RedactionTaskSerializer,
        responses={201: RedactionTaskSerializer}
    )
    def post(self, request):
        """Create a new redaction task"""
        serializer = RedactionTaskSerializer(data=clean_request_data_for_serializer(request.data, RedactionTaskSerializer), context={'request': request})
        if serializer.is_valid():
            redaction_task = serializer.save()

            # Trigger document redaction task
            perform_document_redaction_task.delay(redaction_task.id, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ComplianceAlertView(APIView):
    """
    Manage compliance alerts and violations.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get compliance alerts for a compliance run",
        tags=["Regulatory Compliance - Alerts"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('severity', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('alert_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: ComplianceAlertSerializer(many=True)}
    )
    def get(self, request):
        """Get compliance alerts with optional filtering"""
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)

        alerts = ComplianceAlert.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )

        # Apply filters
        severity = request.query_params.get('severity')
        if severity:
            alerts = alerts.filter(severity=severity)

        alert_status = request.query_params.get('status')
        if alert_status:
            alerts = alerts.filter(status=alert_status)

        alert_type = request.query_params.get('alert_type')
        if alert_type:
            alerts = alerts.filter(alert_type=alert_type)

        serializer = ComplianceAlertSerializer(alerts, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new compliance alert",
        tags=["Regulatory Compliance - Alerts"],
        request_body=ComplianceAlertSerializer,
        responses={201: ComplianceAlertSerializer}
    )
    def post(self, request):
        """Create a new compliance alert"""
        serializer = ComplianceAlertSerializer(data=clean_request_data_for_serializer(request.data, ComplianceAlertSerializer), context={'request': request})
        if serializer.is_valid():
            alert = serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="Update compliance alert status",
        tags=["Regulatory Compliance - Alerts"],
        request_body=ComplianceAlertSerializer,
        responses={200: ComplianceAlertSerializer}
    )
    def put(self, request, pk):
        """Update compliance alert status"""
        alert = get_object_or_404(ComplianceAlert, pk=pk, user=request.user)
        serializer = ComplianceAlertSerializer(alert, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            # Set resolved_at timestamp if status is being changed to resolved
            if request.data.get('status') == 'resolved' and alert.status != 'resolved':
                alert.resolved_at = timezone.now()
                alert.save(update_fields=['resolved_at'])

            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ComplianceSummaryView(APIView):
    """
    Get summary statistics for compliance analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get compliance summary statistics",
        tags=["Regulatory Compliance - Analytics"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: ComplianceSummarySerializer}
    )
    def get(self, request):
        """Get compliance summary statistics"""
        start_time = time.time()
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)

        # Get compliance requirements summary
        requirements = RegulatoryRequirement.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )

        total_requirements = requirements.count()
        compliant_requirements = requirements.filter(compliance_status='compliant').count()
        non_compliant_requirements = requirements.filter(compliance_status='non_compliant').count()
        partially_compliant_requirements = requirements.filter(compliance_status='partially_compliant').count()
        under_review_requirements = requirements.filter(compliance_status='under_review').count()

        compliance_rate = (compliant_requirements / total_requirements * 100) if total_requirements > 0 else 0

        critical_risks = requirements.filter(risk_level='critical').count()
        high_risks = requirements.filter(risk_level='high').count()

        # Category breakdown
        category_breakdown = {}
        for category_choice in RegulatoryRequirement._meta.get_field('category').choices:
            category_code, category_name = category_choice
            count = requirements.filter(category=category_code).count()
            if count > 0:
                category_breakdown[category_name] = count

        # Risk level breakdown
        risk_level_breakdown = {}
        for risk_choice in RegulatoryRequirement._meta.get_field('risk_level').choices:
            risk_code, risk_name = risk_choice
            count = requirements.filter(risk_level=risk_code).count()
            if count > 0:
                risk_level_breakdown[risk_name] = count

        summary_data = {
            'total_requirements': total_requirements,
            'compliant_requirements': compliant_requirements,
            'non_compliant_requirements': non_compliant_requirements,
            'partially_compliant_requirements': partially_compliant_requirements,
            'under_review_requirements': under_review_requirements,
            'compliance_rate': compliance_rate,
            'critical_risks': critical_risks,
            'high_risks': high_risks,
            'category_breakdown': category_breakdown,
            'risk_level_breakdown': risk_level_breakdown
        }

        serializer = ComplianceSummarySerializer(summary_data)
        response_data = {'summary': serializer.data, 'compliance_run_id': compliance_run_id, 'framework': compliance_run.framework}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="Regulatory Compliance Summary",
                    service_id="rc-compliance-summary",
                    vertical="Regulatory Compliance",
                    response_data=response_data,
                    user=request.user,
                    run=compliance_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="compliance-summary-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"framework": compliance_run.framework, "compliance_rate": compliance_rate}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate compliance summary report: {e}")

        return Response(response_data)


class DSARSummaryView(APIView):
    """
    Get summary statistics for DSAR requests.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get DSAR summary statistics",
        tags=["Regulatory Compliance - Analytics"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: DSARSummarySerializer}
    )
    def get(self, request):
        """Get DSAR summary statistics"""
        start_time = time.time()
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)

        # Get DSAR requests summary
        dsar_requests = DSARRequest.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )

        total_requests = dsar_requests.count()
        completed_requests = dsar_requests.filter(status='completed').count()
        overdue_requests = dsar_requests.filter(
            response_due_date__lt=timezone.now(),
            status__in=['received', 'in_progress', 'data_collection', 'review']
        ).count()
        pending_requests = dsar_requests.filter(
            status__in=['received', 'in_progress', 'data_collection', 'review']
        ).count()

        # Calculate average response time for completed requests
        completed_with_response = dsar_requests.filter(
            status='completed',
            response_date__isnull=False
        )

        avg_response_time = 0
        if completed_with_response.exists():
            total_response_time = sum([
                (req.response_date - req.request_date).days
                for req in completed_with_response
            ])
            avg_response_time = total_response_time / completed_with_response.count()

        # Request type breakdown
        request_type_breakdown = {}
        for type_choice in DSARRequest._meta.get_field('request_type').choices:
            type_code, type_name = type_choice
            count = dsar_requests.filter(request_type=type_code).count()
            if count > 0:
                request_type_breakdown[type_name] = count

        # Verification status breakdown
        verification_status_breakdown = {}
        for status_choice in DSARRequest._meta.get_field('verification_status').choices:
            status_code, status_name = status_choice
            count = dsar_requests.filter(verification_status=status_code).count()
            if count > 0:
                verification_status_breakdown[status_name] = count

        summary_data = {
            'total_requests': total_requests,
            'completed_requests': completed_requests,
            'overdue_requests': overdue_requests,
            'pending_requests': pending_requests,
            'avg_response_time': avg_response_time,
            'request_type_breakdown': request_type_breakdown,
            'verification_status_breakdown': verification_status_breakdown
        }

        serializer = DSARSummarySerializer(summary_data)
        response_data = {'summary': serializer.data, 'compliance_run_id': compliance_run_id, 'framework': compliance_run.framework}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="DSAR Request Summary",
                    service_id="rc-dsar-summary",
                    vertical="Regulatory Compliance",
                    response_data=response_data,
                    user=request.user,
                    run=compliance_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="dsar-summary-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"framework": compliance_run.framework, "total_requests": total_requests}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate DSAR summary report: {e}")

        return Response(response_data)


class RedactionSummaryView(APIView):
    """
    Get summary statistics for redaction tasks.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get redaction task summary statistics",
        tags=["Regulatory Compliance - Analytics"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: RedactionSummarySerializer}
    )
    def get(self, request):
        """Get redaction task summary statistics"""
        start_time = time.time()
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)

        # Get redaction tasks summary
        redaction_tasks = RedactionTask.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )

        total_tasks = redaction_tasks.count()
        completed_tasks = redaction_tasks.filter(status='completed').count()
        pending_tasks = redaction_tasks.filter(status='pending').count()
        failed_tasks = redaction_tasks.filter(status='failed').count()

        # Calculate total redactions
        total_redactions = sum([task.redaction_count for task in redaction_tasks])

        # Redaction type breakdown
        redaction_type_breakdown = {}
        for type_choice in RedactionTask._meta.get_field('redaction_type').choices:
            type_code, type_name = type_choice
            count = redaction_tasks.filter(redaction_type=type_code).count()
            if count > 0:
                redaction_type_breakdown[type_name] = count

        # QA completion rate
        qa_required_tasks = redaction_tasks.filter(qa_required=True).count()
        qa_completed_tasks = redaction_tasks.filter(qa_required=True, qa_completed=True).count()
        qa_completion_rate = (qa_completed_tasks / qa_required_tasks * 100) if qa_required_tasks > 0 else 0

        summary_data = {
            'total_tasks': total_tasks,
            'completed_tasks': completed_tasks,
            'pending_tasks': pending_tasks,
            'failed_tasks': failed_tasks,
            'total_redactions': total_redactions,
            'redaction_type_breakdown': redaction_type_breakdown,
            'qa_completion_rate': qa_completion_rate
        }

        serializer = RedactionSummarySerializer(summary_data)
        response_data = {'summary': serializer.data, 'compliance_run_id': compliance_run_id, 'framework': compliance_run.framework}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="Redaction Task Summary",
                    service_id="rc-redaction-summary",
                    vertical="Regulatory Compliance",
                    response_data=response_data,
                    user=request.user,
                    run=compliance_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="redaction-summary-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"framework": compliance_run.framework, "total_redactions": total_redactions}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate redaction summary report: {e}")

        return Response(response_data)


class AlertSummaryView(APIView):
    """
    Get summary statistics for compliance alerts.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get compliance alert summary statistics",
        tags=["Regulatory Compliance - Analytics"],
        manual_parameters=[
            openapi.Parameter('compliance_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: AlertSummarySerializer}
    )
    def get(self, request):
        """Get compliance alert summary statistics"""
        start_time = time.time()
        compliance_run_id = request.query_params.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)

        # Get compliance alerts summary
        alerts = ComplianceAlert.objects.filter(
            compliance_run=compliance_run,
            user=request.user
        )

        total_alerts = alerts.count()
        open_alerts = alerts.filter(status='open').count()
        critical_alerts = alerts.filter(severity='critical').count()

        # Overdue alerts (alerts with due_date in the past and not resolved)
        overdue_alerts = alerts.filter(
            due_date__lt=timezone.now(),
            status__in=['open', 'in_progress']
        ).count()

        # Alert type breakdown
        alert_type_breakdown = {}
        for type_choice in ComplianceAlert._meta.get_field('alert_type').choices:
            type_code, type_name = type_choice
            count = alerts.filter(alert_type=type_code).count()
            if count > 0:
                alert_type_breakdown[type_name] = count

        # Severity breakdown
        severity_breakdown = {}
        for severity_choice in ComplianceAlert._meta.get_field('severity').choices:
            severity_code, severity_name = severity_choice
            count = alerts.filter(severity=severity_code).count()
            if count > 0:
                severity_breakdown[severity_name] = count

        # Resolution rate
        resolved_alerts = alerts.filter(status='resolved').count()
        resolution_rate = (resolved_alerts / total_alerts * 100) if total_alerts > 0 else 0

        summary_data = {
            'total_alerts': total_alerts,
            'open_alerts': open_alerts,
            'critical_alerts': critical_alerts,
            'overdue_alerts': overdue_alerts,
            'alert_type_breakdown': alert_type_breakdown,
            'severity_breakdown': severity_breakdown,
            'resolution_rate': resolution_rate
        }

        serializer = AlertSummarySerializer(summary_data)
        response_data = {'summary': serializer.data, 'compliance_run_id': compliance_run_id, 'framework': compliance_run.framework}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="Compliance Alert Summary",
                    service_id="rc-alert-summary",
                    vertical="Regulatory Compliance",
                    response_data=response_data,
                    user=request.user,
                    run=compliance_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="alert-summary-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"framework": compliance_run.framework, "total_alerts": total_alerts}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate alert summary report: {e}")

        return Response(response_data)


class ComplianceReportView(APIView):
    """
    Generate comprehensive compliance reports.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Generate comprehensive compliance report",
        tags=["Regulatory Compliance - Reports"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'compliance_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'report_type': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    enum=['executive_summary', 'detailed_assessment', 'gap_analysis', 'remediation_plan']
                ),
                'include_sections': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_STRING),
                    description="Sections to include in the report"
                )
            },
            required=['compliance_run_id', 'report_type']
        ),
        responses={202: "Report generation started"}
    )
    def post(self, request):
        """Generate comprehensive compliance report"""
        compliance_run_id = request.data.get('compliance_run_id')
        report_type = request.data.get('report_type')
        include_sections = request.data.get('include_sections', [])

        if not compliance_run_id or not report_type:
            return Response(
                {"error": "compliance_run_id and report_type are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the compliance run
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)

        # Trigger compliance report generation task
        task = generate_compliance_report_task.delay(
            compliance_run.id, report_type, include_sections, request.user.id
        )

        return Response({
            "message": "Compliance report generation started",
            "task_id": task.id,
            "compliance_run_id": compliance_run_id,
            "report_type": report_type
        }, status=status.HTTP_202_ACCEPTED)


class ServiceExecutionListCreateView(APIView):
    """
    API view for listing and creating service executions for Regulatory Compliance.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        """List service executions with optional filtering."""
        try:
            # Get query parameters
            compliance_run_id = request.query_params.get('compliance_run_id')
            service_type = request.query_params.get('service_type')
            status = request.query_params.get('status')

            # Base queryset
            queryset = ServiceExecution.objects.filter(user=request.user)

            # Apply filters
            if compliance_run_id:
                queryset = queryset.filter(compliance_run_id=compliance_run_id)
            if service_type:
                queryset = queryset.filter(service_type=service_type)
            if status:
                queryset = queryset.filter(status=status)

            # Order by most recent
            queryset = queryset.order_by('-started_at')

            # Serialize and return
            data = []
            for execution in queryset:
                data.append({
                    'id': str(execution.id),
                    'compliance_run_id': execution.compliance_run.id if execution.compliance_run else None,
                    'service_type': execution.service_type,
                    'service_name': execution.service_name,
                    'service_version': execution.service_version,
                    'status': execution.status,
                    'started_at': execution.started_at.isoformat(),
                    'completed_at': execution.completed_at.isoformat() if execution.completed_at else None,
                    'execution_time_seconds': execution.execution_time_seconds,
                    'input_files': execution.input_files,
                    'input_parameters': execution.input_parameters,
                    'output_type': execution.output_type,
                    'output_count': execution.output_count,
                    'error_message': execution.error_message,
                    'execution_metadata': execution.execution_metadata,
                })

            return Response({
                'success': True,
                'data': data,
                'count': len(data)
            })

        except Exception as e:
            logger.error(f"Error listing service executions: {str(e)}")
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        """Create a new service execution record."""
        try:
            data = request.data

            # Validate required fields
            required_fields = ['service_type', 'service_name']
            for field in required_fields:
                if field not in data:
                    return Response({
                        'success': False,
                        'error': f'Missing required field: {field}'
                    }, status=status.HTTP_400_BAD_REQUEST)

            # Get compliance run if provided
            compliance_run = None
            if 'compliance_run_id' in data:
                try:
                    compliance_run = ComplianceRun.objects.get(
                        id=data['compliance_run_id'],
                        user=request.user
                    )
                except ComplianceRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': 'Compliance run not found'
                    }, status=status.HTTP_404_NOT_FOUND)

            # Create service execution
            execution = ServiceExecution.objects.create(
                user=request.user,
                client=get_user_client(request.user),
                compliance_run=compliance_run,
                service_type=data['service_type'],
                service_name=data['service_name'],
                service_version=data.get('service_version', '1.0'),
                status=data.get('status', 'pending'),
                input_files=data.get('input_files', []),
                input_parameters=data.get('input_parameters', {}),
                output_type=data.get('output_type', 'json'),
                output_count=data.get('output_count', 0),
                execution_metadata=data.get('execution_metadata', {})
            )

            return Response({
                'success': True,
                'data': {
                    'id': str(execution.id),
                    'service_type': execution.service_type,
                    'service_name': execution.service_name,
                    'status': execution.status,
                    'started_at': execution.started_at.isoformat()
                }
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error creating service execution: {str(e)}")
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ServiceOutputListCreateView(APIView):
    """
    API view for listing and creating service outputs for Regulatory Compliance.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        """List service outputs with optional filtering."""
        try:
            # Get query parameters
            service_execution_id = request.query_params.get('service_execution_id')
            output_type = request.query_params.get('output_type')

            # Base queryset
            queryset = ServiceOutput.objects.filter(
                service_execution__user=request.user
            )

            # Apply filters
            if service_execution_id:
                queryset = queryset.filter(service_execution_id=service_execution_id)
            if output_type:
                queryset = queryset.filter(output_type=output_type)

            # Order by most recent
            queryset = queryset.order_by('-created_at')

            # Serialize and return
            data = []
            for output in queryset:
                data.append({
                    'id': str(output.id),
                    'service_execution_id': str(output.service_execution.id),
                    'output_name': output.output_name,
                    'output_type': output.output_type,
                    'file_extension': output.file_extension,
                    'mime_type': output.mime_type,
                    'file_size': output.file_size,
                    'download_url': output.download_url,
                    'preview_url': output.preview_url,
                    'is_primary': output.is_primary,
                    'created_at': output.created_at.isoformat(),
                    'output_metadata': output.output_metadata,
                })

            return Response({
                'success': True,
                'data': data,
                'count': len(data)
            })

        except Exception as e:
            logger.error(f"Error listing service outputs: {str(e)}")
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        """Create a new service output record."""
        try:
            data = request.data

            # Validate required fields
            required_fields = ['service_execution_id', 'output_name', 'output_type']
            for field in required_fields:
                if field not in data:
                    return Response({
                        'success': False,
                        'error': f'Missing required field: {field}'
                    }, status=status.HTTP_400_BAD_REQUEST)

            # Get service execution
            try:
                service_execution = ServiceExecution.objects.get(
                    id=data['service_execution_id'],
                    user=request.user
                )
            except ServiceExecution.DoesNotExist:
                return Response({
                    'success': False,
                    'error': 'Service execution not found'
                }, status=status.HTTP_404_NOT_FOUND)

            # Create service output
            output = ServiceOutput.objects.create(
                user=request.user,
                client=get_user_client(request.user),
                service_execution=service_execution,
                output_name=data['output_name'],
                output_type=data['output_type'],
                file_extension=data.get('file_extension', ''),
                mime_type=data.get('mime_type', ''),
                file_size=data.get('file_size'),
                output_data=data.get('output_data'),
                output_text=data.get('output_text', ''),
                download_url=data.get('download_url', ''),
                preview_url=data.get('preview_url', ''),
                is_primary=data.get('is_primary', False),
                output_metadata=data.get('output_metadata', {})
            )

            return Response({
                'success': True,
                'data': {
                    'id': str(output.id),
                    'output_name': output.output_name,
                    'output_type': output.output_type,
                    'created_at': output.created_at.isoformat()
                }
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error creating service output: {str(e)}")
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class GapAnalysisView(APIView):
    """
    POST /api/v1/regulatory-compliance/gap-analysis/
    Trigger a gap analysis for a compliance run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def post(self, request):
        compliance_run_id = request.data.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)
        return Response({
            "message": "Gap analysis started",
            "compliance_run_id": compliance_run_id
        }, status=status.HTTP_202_ACCEPTED)


class AuditPreparationView(APIView):
    """
    POST /api/v1/regulatory-compliance/audit-preparation/
    Trigger audit preparation for a compliance run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def post(self, request):
        compliance_run_id = request.data.get('compliance_run_id')
        if not compliance_run_id:
            return Response({"error": "compliance_run_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        compliance_run = get_object_or_404(ComplianceRun, pk=compliance_run_id, run__user=request.user)
        return Response({
            "message": "Audit preparation started",
            "compliance_run_id": compliance_run_id
        }, status=status.HTTP_202_ACCEPTED)
