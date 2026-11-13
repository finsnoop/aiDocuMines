import time
import logging

from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Avg, F
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.parsers import MultiPartParser, FormParser
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from core.utils import generate_and_register_service_report, get_datetime_folder_from_dd_run, clean_request_data_for_serializer, get_logical_path

from custom_authentication.permissions import IsClientOrAdmin, IsClientOrAdminOrSuperUser
from core.vertical_permissions import IsClientMember, IsClientAdmin, IsOwnerOrClientAdmin
from core.models import File
from .models import (
    DueDiligenceRun, DocumentClassification, RiskClause,
    FindingsReport, DataRoomConnector, ServiceExecution, ServiceOutput,
    ClosingChecklist, PostCloseObligation, DealVelocityMetrics, ClauseLibrary,
    # New PE models
    PanelFirm, RFP, RFPBid, EngagementLetter,
    SignatureTracker, ConditionPrecedent, ClosingBinder,
    Covenant, ConsentFiling
)
from .serializers import (
    DueDiligenceRunSerializer, DueDiligenceRunCreateSerializer,
    DocumentClassificationSerializer, RiskClauseSerializer,
    FindingsReportSerializer, DataRoomConnectorSerializer,
    RiskClauseSummarySerializer, DocumentTypeSummarySerializer,
    ClosingChecklistSerializer, ClosingChecklistCreateSerializer,
    PostCloseObligationSerializer, PostCloseObligationCreateSerializer,
    DealVelocityMetricsSerializer, ClauseLibrarySerializer, ClauseLibraryCreateSerializer,
    DealVelocitySummarySerializer, ChecklistProgressSerializer, PostCloseObligationSummarySerializer,
    # New PE serializers
    PanelFirmSerializer, RFPSerializer, RFPBidSerializer, EngagementLetterSerializer,
    SignatureTrackerSerializer, ConditionPrecedentSerializer, ClosingBinderSerializer,
    CovenantSerializer, ConsentFilingSerializer,
    PortfolioComplianceSerializer, RiskHeatmapSerializer, BidAnalysisSerializer
)
from .tasks import (
    classify_document_task, extract_risk_clauses_task,
    generate_findings_report_task, sync_data_room_task
)

logger = logging.getLogger(__name__)

# Import AI services for enhanced document processing
try:
    from document_search.tasks import semantic_search_task, index_file
    from file_elasticsearch.utils import search_files
    AI_SERVICES_AVAILABLE = True
except ImportError:
    AI_SERVICES_AVAILABLE = False
    logger.warning("AI services not available - falling back to basic processing")


class DueDiligenceRunListCreateView(APIView):
    """
    List all due diligence runs for the authenticated user or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="List all due diligence runs for the authenticated user",
        tags=["Private Equity - Due Diligence"],
        responses={200: DueDiligenceRunSerializer(many=True)}
    )
    def get(self, request):
        """List all due diligence runs for the user's client"""
        # Filter by client for multi-tenancy
        runs = DueDiligenceRun.objects.filter(
            client=request.user.client,
            run__user=request.user
        )
        serializer = DueDiligenceRunSerializer(runs, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Create a new due diligence run",
        tags=["Private Equity - Due Diligence"],
        request_body=DueDiligenceRunCreateSerializer,
        responses={201: DueDiligenceRunSerializer}
    )
    def post(self, request):
        """Create a new due diligence run"""
        from core.utils import clean_request_data_for_serializer
        serializer = DueDiligenceRunCreateSerializer(data=clean_request_data_for_serializer(request.data, DueDiligenceRunCreateSerializer), context={'request': request})
        if serializer.is_valid():
            due_diligence_run = serializer.save()
            response_serializer = DueDiligenceRunSerializer(due_diligence_run)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DueDiligenceRunDetailView(APIView):
    """
    Retrieve, update or delete a specific due diligence run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    def get_object(self, pk, user):
        """Get due diligence run ensuring client ownership"""
        return get_object_or_404(
            DueDiligenceRun,
            pk=pk,
            client=user.client,
            run__user=user
        )
    
    @swagger_auto_schema(
        operation_description="Retrieve a specific due diligence run",
        tags=["Private Equity - Due Diligence"],
        responses={200: DueDiligenceRunSerializer}
    )
    def get(self, request, pk):
        """Retrieve a specific due diligence run"""
        dd_run = self.get_object(pk, request.user)
        serializer = DueDiligenceRunSerializer(dd_run)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Update a specific due diligence run",
        tags=["Private Equity - Due Diligence"],
        request_body=DueDiligenceRunSerializer,
        responses={200: DueDiligenceRunSerializer}
    )
    def put(self, request, pk):
        """Update a specific due diligence run"""
        dd_run = self.get_object(pk, request.user)
        serializer = DueDiligenceRunSerializer(dd_run, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @swagger_auto_schema(
        operation_description="Delete a specific due diligence run",
        tags=["Private Equity - Due Diligence"],
        responses={204: "No Content"}
    )
    def delete(self, request, pk):
        """Delete a specific due diligence run"""
        dd_run = self.get_object(pk, request.user)
        dd_run.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DocumentClassificationView(APIView):
    """
    Auto-classify documents or retrieve classification results.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get document classifications for a due diligence run",
        tags=["Private Equity - Document Classification"],
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: DocumentClassificationSerializer(many=True)}
    )
    def get(self, request):
        """Get document classifications for a due diligence run"""
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({"error": "dd_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)
        
        classifications = DocumentClassification.objects.filter(
            due_diligence_run=dd_run,
            user=request.user
        )
        serializer = DocumentClassificationSerializer(classifications, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Trigger document classification for uploaded files",
        tags=["Private Equity - Document Classification"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'dd_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'file_ids': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_INTEGER))
            },
            required=['dd_run_id', 'file_ids']
        ),
        responses={202: "Classification started"}
    )
    def post(self, request):
        """Trigger document classification for uploaded files"""
        dd_run_id = request.data.get('due_diligence_run') or request.data.get('dd_run_id')
        file_ids = request.data.get('file_ids', [])
        # ✅ Accept project_id, service_id, and datetime_folder from frontend to preserve tree location
        project_id = request.data.get('project_id')
        service_id = request.data.get('service_id')
        datetime_folder = request.data.get('datetime_folder')

        logger.info(f"📋 DocumentClassificationView.post - project_id={project_id}, service_id={service_id}, datetime_folder={datetime_folder}")

        if not dd_run_id or not file_ids:
            return Response(
                {"error": "dd_run_id and file_ids are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)

        # Ensure user owns all files
        files = File.objects.filter(id__in=file_ids, user=request.user)
        if files.count() != len(file_ids):
            return Response(
                {"error": "Some files not found or not owned by user"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ✅ If project_id/service_id/datetime_folder not provided, derive from first file
        if files.exists():
            first_file = files.first()
            if not project_id:
                project_id = first_file.project_id
            if not service_id:
                service_id = first_file.service_id
            # ✅ Extract datetime_folder from file path if not provided
            if not datetime_folder and first_file.filepath:
                # Path structure: uploads/client/user/PROJECT_ID/SERVICE_ID/DATETIME/...
                path_parts = first_file.filepath.split('/')
                if 'uploads' in path_parts:
                    uploads_idx = path_parts.index('uploads')
                    if len(path_parts) > uploads_idx + 5:
                        datetime_folder = path_parts[uploads_idx + 5]
                        logger.info(f"📋 Derived datetime_folder from file path: {datetime_folder}")

        # Trigger classification tasks with project context
        task_ids = []
        for file_obj in files:
            task = classify_document_task.delay(
                file_obj.id, dd_run.id, request.user.id,
                project_id=project_id, service_id=service_id, datetime_folder=datetime_folder
            )
            task_ids.append(task.id)

        return Response({
            "message": "Document classification started",
            "task_ids": task_ids,
            "files_count": len(file_ids)
        }, status=status.HTTP_202_ACCEPTED)


class RiskClauseExtractionView(APIView):
    """
    Extract and manage risk clauses from documents.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get risk clauses for a due diligence run",
        tags=["Private Equity - Risk Analysis"],
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('risk_level', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('clause_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: RiskClauseSerializer(many=True)}
    )
    def get(self, request):
        """Get risk clauses for a due diligence run with optional filtering"""
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({"error": "dd_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)
        
        risk_clauses = RiskClause.objects.filter(
            due_diligence_run=dd_run,
            user=request.user
        )
        
        # Apply filters
        risk_level = request.query_params.get('risk_level')
        if risk_level:
            risk_clauses = risk_clauses.filter(risk_level=risk_level)
        
        clause_type = request.query_params.get('clause_type')
        if clause_type:
            risk_clauses = risk_clauses.filter(clause_type=clause_type)
        
        serializer = RiskClauseSerializer(risk_clauses, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Trigger risk clause extraction for documents",
        tags=["Private Equity - Risk Analysis"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'dd_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'file_ids': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_INTEGER))
            },
            required=['dd_run_id', 'file_ids']
        ),
        responses={202: "Risk extraction started"}
    )
    def post(self, request):
        """Trigger risk clause extraction for documents"""
        dd_run_id = request.data.get('due_diligence_run') or request.data.get('dd_run_id')
        file_ids = request.data.get('file_ids', [])
        # ✅ Accept project_id, service_id, and datetime_folder from frontend to preserve tree location
        project_id = request.data.get('project_id')
        service_id = request.data.get('service_id')
        datetime_folder = request.data.get('datetime_folder')

        logger.info(f"📋 RiskClauseExtractionView.post - project_id={project_id}, service_id={service_id}, datetime_folder={datetime_folder}")

        if not dd_run_id or not file_ids:
            return Response(
                {"error": "dd_run_id and file_ids are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)

        # Ensure user owns all files
        files = File.objects.filter(id__in=file_ids, user=request.user)
        if files.count() != len(file_ids):
            return Response(
                {"error": "Some files not found or not owned by user"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ✅ If project_id/service_id/datetime_folder not provided, derive from first file
        if files.exists():
            first_file = files.first()
            if not project_id:
                project_id = first_file.project_id
            if not service_id:
                service_id = first_file.service_id
            # ✅ Extract datetime_folder from file path if not provided
            if not datetime_folder and first_file.filepath:
                path_parts = first_file.filepath.split('/')
                if 'uploads' in path_parts:
                    uploads_idx = path_parts.index('uploads')
                    if len(path_parts) > uploads_idx + 5:
                        datetime_folder = path_parts[uploads_idx + 5]
                        logger.info(f"📋 Derived datetime_folder from file path: {datetime_folder}")

        # Trigger risk extraction tasks with project context
        task_ids = []
        for file_obj in files:
            task = extract_risk_clauses_task.delay(
                file_obj.id, dd_run.id, request.user.id,
                project_id=project_id, service_id=service_id, datetime_folder=datetime_folder
            )
            task_ids.append(task.id)

        return Response({
            "message": "Risk clause extraction started",
            "task_ids": task_ids,
            "files_count": len(file_ids)
        }, status=status.HTTP_202_ACCEPTED)


class IssueSpottingView(APIView):
    """
    AI-powered issue spotting for deal documents.
    Scans documents for critical issues, risks, and red flags.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Trigger AI issue spotting for deal documents",
        tags=["Private Equity - Issue Spotting"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['deal_workspace_id'],
            properties={
                'deal_workspace_id': openapi.Schema(type=openapi.TYPE_INTEGER, description='Deal workspace ID'),
                'file_ids': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_INTEGER)),
                'issue_categories': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_STRING),
                    description='Issue categories to scan for (e.g., change_of_control, assignment_restrictions, mac_clauses)'
                ),
                'severity_threshold': openapi.Schema(type=openapi.TYPE_STRING, enum=['low', 'medium', 'high', 'critical']),
            }
        ),
        responses={202: "Issue spotting started"}
    )
    def post(self, request):
        """Trigger AI issue spotting for deal documents"""
        deal_workspace_id = request.data.get('due_diligence_run') or request.data.get('deal_workspace_id')
        file_ids = request.data.get('file_ids', [])
        # ✅ Accept project_id, service_id, and datetime_folder from frontend to preserve tree location
        project_id = request.data.get('project_id')
        service_id = request.data.get('service_id')
        datetime_folder = request.data.get('datetime_folder')
        issue_categories = request.data.get('issue_categories', [
            'change_of_control', 'assignment_restrictions', 'mac_clauses',
            'termination_rights', 'indemnification', 'non_compete',
            'consent_requirements', 'financial_covenants'
        ])
        severity_threshold = request.data.get('severity_threshold', 'low')

        logger.info(f"📋 IssueSpottingView.post - project_id={project_id}, service_id={service_id}, datetime_folder={datetime_folder}")

        if not deal_workspace_id:
            return Response(
                {"error": "deal_workspace_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the deal workspace
        dd_run = get_object_or_404(DueDiligenceRun, pk=deal_workspace_id, client=request.user.client, client__isnull=False)

        # If no specific files, get all files for the DD run
        if file_ids:
            files = File.objects.filter(id__in=file_ids, user=request.user)
        else:
            # Get files associated with this DD run via DocumentClassification
            classified_file_ids = DocumentClassification.objects.filter(
                due_diligence_run=dd_run,
                user=request.user
            ).values_list('file_id', flat=True)
            files = File.objects.filter(id__in=classified_file_ids)

        if not files.exists():
            return Response(
                {"error": "No files found for issue spotting. Please classify documents first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ✅ If project_id/service_id/datetime_folder not provided, derive from first file
        if files.exists():
            first_file = files.first()
            if not project_id:
                project_id = first_file.project_id
            if not service_id:
                service_id = first_file.service_id
            # ✅ Extract datetime_folder from file path if not provided
            if not datetime_folder and first_file.filepath:
                path_parts = first_file.filepath.split('/')
                if 'uploads' in path_parts:
                    uploads_idx = path_parts.index('uploads')
                    if len(path_parts) > uploads_idx + 5:
                        datetime_folder = path_parts[uploads_idx + 5]
                        logger.info(f"📋 Derived datetime_folder from file path: {datetime_folder}")

        # Trigger risk extraction tasks for issue spotting with project context
        task_ids = []
        for file_obj in files:
            task = extract_risk_clauses_task.delay(
                file_obj.id, dd_run.id, request.user.id,
                project_id=project_id, service_id=service_id, datetime_folder=datetime_folder
            )
            task_ids.append(task.id)

        return Response({
            "message": "AI issue spotting started",
            "task_ids": task_ids,
            "files_count": files.count(),
            "deal_workspace_id": deal_workspace_id,
            "issue_categories": issue_categories,
            "severity_threshold": severity_threshold,
            "status": "processing"
        }, status=status.HTTP_202_ACCEPTED)

    @swagger_auto_schema(
        operation_description="Get issue spotting results for a deal workspace",
        tags=["Private Equity - Issue Spotting"],
        manual_parameters=[
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('severity', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('category', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: RiskClauseSerializer(many=True)}
    )
    def get(self, request):
        """Get issue spotting results (risk clauses) for a deal workspace"""
        start_time = time.time()
        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if not deal_workspace_id:
            return Response(
                {"error": "deal_workspace_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the deal workspace
        dd_run = get_object_or_404(DueDiligenceRun, pk=deal_workspace_id, client=request.user.client, client__isnull=False)

        # Get risk clauses (issues) for this workspace
        issues = RiskClause.objects.filter(
            due_diligence_run=dd_run,
            user=request.user
        )

        # Apply filters
        severity = request.query_params.get('severity')
        if severity:
            issues = issues.filter(risk_level=severity)

        category = request.query_params.get('category')
        if category:
            issues = issues.filter(clause_type=category)

        # Build response with summary
        issues_data = RiskClauseSerializer(issues, many=True).data

        summary = {
            'total_issues': issues.count(),
            'critical_count': issues.filter(risk_level='critical').count(),
            'high_count': issues.filter(risk_level='high').count(),
            'medium_count': issues.filter(risk_level='medium').count(),
            'low_count': issues.filter(risk_level='low').count(),
        }

        response_data = {
            'deal_workspace_id': deal_workspace_id,
            'deal_name': dd_run.deal_name,
            'summary': summary,
            'issues': issues_data
        }

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                # Get a reference file to use its run (for proper file tree association)
                # This ensures the report appears in the same folder structure as the source files
                files = File.objects.filter(
                    pe_classifications__due_diligence_run=dd_run,
                    user=request.user,
                ).distinct()
                reference_file = files.first()
                upload_run = reference_file.run if reference_file else dd_run.run

                # Extract datetime_folder from DD run's files to maintain folder structure
                datetime_folder = request.query_params.get('datetime_folder') or get_datetime_folder_from_dd_run(dd_run)
                report_info = generate_and_register_service_report(
                    service_name="PE Issue Spotting Analysis",
                    service_id="pe-issue-spotting",
                    vertical="Private Equity",
                    response_data=response_data,
                    user=request.user,
                    run=upload_run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="issue-spotting-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={
                        "deal_name": dd_run.deal_name,
                        "total_issues": summary['total_issues'],
                        "critical_count": summary['critical_count']
                    },
                    datetime_folder=datetime_folder
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate issue spotting report: {e}")

        return Response(response_data)


class FindingsReportView(APIView):
    """
    Generate and manage findings reports for due diligence runs.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get findings reports for a due diligence run",
        tags=["Private Equity - Reports"],
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: FindingsReportSerializer(many=True)}
    )
    def get(self, request):
        """Get findings reports for a due diligence run"""
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({"error": "dd_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)

        reports = FindingsReport.objects.filter(
            due_diligence_run=dd_run,
            user=request.user
        )
        serializer = FindingsReportSerializer(reports, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Generate a new findings report",
        tags=["Private Equity - Reports"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'dd_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'report_name': openapi.Schema(type=openapi.TYPE_STRING),
                'project_id': openapi.Schema(type=openapi.TYPE_STRING, description="Project ID for file registration"),
                'service_id': openapi.Schema(type=openapi.TYPE_STRING, description="Service ID for file registration"),
                'file_id': openapi.Schema(type=openapi.TYPE_INTEGER, description="Reference file ID to derive project context")
            },
            required=['dd_run_id']
        ),
        responses={202: "Report generation started"}
    )
    def post(self, request):
        """Generate a new findings report"""
        dd_run_id = request.data.get('due_diligence_run') or request.data.get('dd_run_id')
        report_name = request.data.get('report_name', 'Due Diligence Findings Report')
        project_id = request.data.get('project_id')
        service_id = request.data.get('service_id')
        file_id = request.data.get('file_id')
        datetime_folder = request.data.get('datetime_folder')  # ✅ Extract datetime_folder from frontend

        logger.info(f"📋 FindingsReportView.post - Received request:")
        logger.info(f"   dd_run_id={dd_run_id}, project_id={project_id}, service_id={service_id}, file_id={file_id}, datetime_folder={datetime_folder}")
        logger.info(f"   Full request.data: {request.data}")

        if not dd_run_id:
            return Response(
                {"error": "dd_run_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)

        # Trigger report generation task with project context
        logger.info(f"📋 Triggering generate_findings_report_task with project_id={project_id}, service_id={service_id}, datetime_folder={datetime_folder}")
        task = generate_findings_report_task.delay(
            dd_run.id,
            request.user.id,
            report_name,
            project_id=project_id,
            service_id=service_id,
            file_id=file_id,
            datetime_folder=datetime_folder  # ✅ Pass datetime_folder to task
        )

        return Response({
            "message": "Findings report generation started",
            "task_id": task.id,
            "dd_run_id": dd_run_id
        }, status=status.HTTP_202_ACCEPTED)


class SyncDataRoomView(APIView):
    """
    Sync documents from a data room (Google Drive, SharePoint, etc.) to a deal workspace.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List data room connectors for a deal workspace",
        tags=["Private Equity - Data Room"],
        manual_parameters=[
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: DataRoomConnectorSerializer(many=True)}
    )
    def get(self, request):
        """List data room connectors for a deal workspace"""
        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if not deal_workspace_id:
            return Response({"error": "deal_workspace_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the deal workspace
        dd_run = get_object_or_404(DueDiligenceRun, pk=deal_workspace_id, client=request.user.client, client__isnull=False)

        connectors = DataRoomConnector.objects.filter(
            due_diligence_run=dd_run,
            user=request.user,
            is_active=True
        )

        serializer = DataRoomConnectorSerializer(connectors, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a data room connector and trigger sync",
        tags=["Private Equity - Data Room"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'deal_workspace_id': openapi.Schema(type=openapi.TYPE_INTEGER, description='Deal workspace ID'),
                'connector_type': openapi.Schema(type=openapi.TYPE_STRING, enum=['google_drive', 'sharepoint', 'box', 'dropbox']),
                'connector_name': openapi.Schema(type=openapi.TYPE_STRING),
                'connection_config': openapi.Schema(type=openapi.TYPE_OBJECT),
                'folder_path': openapi.Schema(type=openapi.TYPE_STRING, description='Path to sync from'),
            },
            required=['deal_workspace_id', 'connector_type']
        ),
        responses={202: "Data room sync started"}
    )
    def post(self, request):
        """Create a data room connector and trigger sync"""
        deal_workspace_id = request.data.get('due_diligence_run') or request.data.get('deal_workspace_id')
        connector_type = request.data.get('connector_type', 'google_drive')
        connector_name = request.data.get('connector_name', f'{connector_type.replace("_", " ").title()} Sync')
        connection_config = request.data.get('connection_config', {})
        folder_path = request.data.get('folder_path', '/')

        if not deal_workspace_id:
            return Response(
                {"error": "deal_workspace_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the deal workspace
        dd_run = get_object_or_404(DueDiligenceRun, pk=deal_workspace_id, client=request.user.client, client__isnull=False)

        # Create or update the connector
        connector, created = DataRoomConnector.objects.update_or_create(
            due_diligence_run=dd_run,
            user=request.user,
            connector_name=connector_name,
            defaults={
                'client': request.user.client,
                'connector_type': connector_type,
                'connection_config': connection_config,
                'sync_status': 'pending',
                'is_active': True
            }
        )

        # Trigger the sync task
        try:
            task = sync_data_room_task.delay(connector.id, request.user.id, folder_path)

            return Response({
                "message": "Data room sync started",
                "connector_id": connector.id,
                "task_id": task.id,
                "deal_workspace_id": deal_workspace_id,
                "connector_type": connector_type,
                "status": "pending"
            }, status=status.HTTP_202_ACCEPTED)
        except Exception as e:
            logger.error(f"Failed to start data room sync: {str(e)}")
            return Response({
                "message": "Data room sync initiated (task queued)",
                "connector_id": connector.id,
                "deal_workspace_id": deal_workspace_id,
                "connector_type": connector_type,
                "status": "queued",
                "note": "Sync will be processed when the task worker is available"
            }, status=status.HTTP_202_ACCEPTED)


class RiskClauseSummaryView(APIView):
    """
    Get summary statistics for risk clauses in a due diligence run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get risk clause summary statistics",
        tags=["Private Equity - Analytics"],
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: RiskClauseSummarySerializer(many=True)}
    )
    def get(self, request):
        """Get risk clause summary statistics for a due diligence run"""
        start_time = time.time()
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({"error": "dd_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)

        # Get summary statistics grouped by clause type
        summary_data = []
        clause_types = RiskClause.objects.filter(
            due_diligence_run=dd_run,
            user=request.user
        ).values_list('clause_type', flat=True).distinct()

        for clause_type in clause_types:
            clauses = RiskClause.objects.filter(
                due_diligence_run=dd_run,
                user=request.user,
                clause_type=clause_type
            )

            summary_data.append({
                'clause_type': clause_type,
                'clause_type_display': dict(RiskClause._meta.get_field('clause_type').choices)[clause_type],
                'total_count': clauses.count(),
                'high_risk_count': clauses.filter(risk_level='high').count(),
                'medium_risk_count': clauses.filter(risk_level='medium').count(),
                'low_risk_count': clauses.filter(risk_level='low').count(),
                'critical_risk_count': clauses.filter(risk_level='critical').count(),
            })

        serializer = RiskClauseSummarySerializer(summary_data, many=True)
        response_data = {'summary': serializer.data, 'dd_run_id': dd_run_id, 'deal_name': dd_run.deal_name}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                # Get a reference file to use its run (for proper file tree association)
                first_risk_clause = RiskClause.objects.filter(
                    due_diligence_run=dd_run, user=request.user
                ).select_related('file').first()
                reference_file = first_risk_clause.file if first_risk_clause else None
                upload_run = reference_file.run if reference_file else dd_run.run

                datetime_folder = request.query_params.get('datetime_folder') or get_datetime_folder_from_dd_run(dd_run)
                report_info = generate_and_register_service_report(
                    service_name="PE Risk Clause Summary",
                    service_id="pe-risk-summary",
                    vertical="Private Equity",
                    response_data=response_data,
                    user=request.user,
                    run=upload_run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="risk-summary-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"deal_name": dd_run.deal_name, "clause_types_count": len(summary_data)},
                    datetime_folder=datetime_folder
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate risk summary report: {e}")

        return Response(response_data)


class DocumentTypeSummaryView(APIView):
    """
    Get summary statistics for document classifications in a due diligence run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get document type summary statistics",
        tags=["Private Equity - Analytics"],
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: DocumentTypeSummarySerializer(many=True)}
    )
    def get(self, request):
        """Get document type summary statistics for a due diligence run"""
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({"error": "dd_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the DD run
        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, run__user=request.user)

        # Get summary statistics grouped by document type
        summary_data = []
        doc_types = DocumentClassification.objects.filter(
            due_diligence_run=dd_run,
            user=request.user
        ).values_list('document_type', flat=True).distinct()

        for doc_type in doc_types:
            classifications = DocumentClassification.objects.filter(
                due_diligence_run=dd_run,
                user=request.user,
                document_type=doc_type
            )

            summary_data.append({
                'document_type': doc_type,
                'document_type_display': dict(DocumentClassification._meta.get_field('document_type').choices)[doc_type],
                'total_count': classifications.count(),
                'verified_count': classifications.filter(is_verified=True).count(),
                'avg_confidence_score': classifications.aggregate(Avg('confidence_score'))['confidence_score__avg'] or 0.0,
            })

        serializer = DocumentTypeSummarySerializer(summary_data, many=True)
        return Response(serializer.data)


# ═══════════════════════════════════════════════════════════════
# 🔄 SERVICE EXECUTION & OUTPUT PERSISTENCE VIEWS
# ═══════════════════════════════════════════════════════════════

class ServiceExecutionListCreateView(APIView):
    """
    List and create service executions for Private Equity services.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all service executions for the authenticated user",
        tags=["Private Equity - Service Tracking"],
        responses={200: "List of service executions"}
    )
    def get(self, request):
        """List all service executions for the authenticated user"""
        executions = ServiceExecution.objects.filter(user=request.user)

        # Filter by due diligence run if provided
        dd_run_id = request.query_params.get('due_diligence_run')
        if dd_run_id:
            executions = executions.filter(due_diligence_run_id=dd_run_id)

        # Filter by service type if provided
        service_type = request.query_params.get('service_type')
        if service_type:
            executions = executions.filter(service_type=service_type)

        # Filter by status if provided
        status_filter = request.query_params.get('status')
        if status_filter:
            executions = executions.filter(status=status_filter)

        # Serialize and return
        data = []
        for execution in executions:
            data.append({
                'id': execution.id,
                'service_type': execution.service_type,
                'service_name': execution.service_name,
                'status': execution.status,
                'started_at': execution.started_at,
                'completed_at': execution.completed_at,
                'duration': execution.duration,
                'output_count': execution.output_count,
                'output_type': execution.output_type,
                'due_diligence_run': execution.due_diligence_run.id,
                'execution_metadata': execution.execution_metadata
            })

        return Response(data)

    @swagger_auto_schema(
        operation_description="Create a new service execution record",
        tags=["Private Equity - Service Tracking"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'service_type': openapi.Schema(type=openapi.TYPE_STRING),
                'service_name': openapi.Schema(type=openapi.TYPE_STRING),
                'due_diligence_run': openapi.Schema(type=openapi.TYPE_INTEGER),
                'input_parameters': openapi.Schema(type=openapi.TYPE_OBJECT),
                'output_type': openapi.Schema(type=openapi.TYPE_STRING),
                'execution_metadata': openapi.Schema(type=openapi.TYPE_OBJECT),
            }
        ),
        responses={201: "Service execution created"}
    )
    def post(self, request):
        """Create a new service execution record"""
        try:
            # Get or create due diligence run
            dd_run_id = request.data.get('due_diligence_run')
            if dd_run_id:
                dd_run = get_object_or_404(DueDiligenceRun, id=dd_run_id, run__user=request.user)
            else:
                # Create a default DD run if none provided
                from core.models import Run, Project
                project = Project.objects.filter(user=request.user).first()
                if not project:
                    return Response(
                        {'error': 'No project found. Please create a project first.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                run = Run.objects.create(
                    user=request.user,
                    project=project,
                    name=f"Service Execution Run - {request.data.get('service_name', 'Unknown')}"
                )

                dd_run = DueDiligenceRun.objects.create(
                    run=run,
                    deal_name=f"Auto-generated for {request.data.get('service_name', 'Service')}",
                    target_company="Auto-generated"
                )

            # Create service execution
            execution = ServiceExecution.objects.create(
                user=request.user,
                due_diligence_run=dd_run,
                service_type=request.data.get('service_type'),
                service_name=request.data.get('service_name'),
                service_version=request.data.get('service_version', '1.0'),
                status=request.data.get('status', 'completed'),
                input_parameters=request.data.get('input_parameters', {}),
                output_type=request.data.get('output_type', 'json'),
                output_count=request.data.get('output_count', 1),
                execution_metadata=request.data.get('execution_metadata', {})
            )

            # Set completed_at if status is completed
            if execution.status == 'completed':
                from django.utils import timezone
                execution.completed_at = timezone.now()
                execution.save()

            return Response({
                'id': execution.id,
                'service_type': execution.service_type,
                'service_name': execution.service_name,
                'status': execution.status,
                'created_at': execution.created_at
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error creating service execution: {str(e)}")
            return Response(
                {'error': f'Failed to create service execution: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )


class ServiceOutputListCreateView(APIView):
    """
    List and create service outputs for Private Equity services.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all service outputs for the authenticated user",
        tags=["Private Equity - Service Tracking"],
        responses={200: "List of service outputs"}
    )
    def get(self, request):
        """List all service outputs for the authenticated user"""
        outputs = ServiceOutput.objects.filter(service_execution__user=request.user)

        # Filter by service execution if provided
        execution_id = request.query_params.get('service_execution')
        if execution_id:
            outputs = outputs.filter(service_execution_id=execution_id)

        # Filter by output type if provided
        output_type = request.query_params.get('output_type')
        if output_type:
            outputs = outputs.filter(output_type=output_type)

        # Serialize and return
        data = []
        for output in outputs:
            data.append({
                'id': output.id,
                'service_execution': output.service_execution.id,
                'output_name': output.output_name,
                'output_type': output.output_type,
                'file_size': output.file_size,
                'formatted_size': output.formatted_size,
                'is_primary': output.is_primary,
                'created_at': output.created_at,
                'download_url': output.download_url,
                'preview_url': output.preview_url,
                'output_metadata': output.output_metadata
            })

        return Response(data)

    @swagger_auto_schema(
        operation_description="Create a new service output record",
        tags=["Private Equity - Service Tracking"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'service_execution': openapi.Schema(type=openapi.TYPE_STRING),
                'output_name': openapi.Schema(type=openapi.TYPE_STRING),
                'output_type': openapi.Schema(type=openapi.TYPE_STRING),
                'output_data': openapi.Schema(type=openapi.TYPE_OBJECT),
                'output_text': openapi.Schema(type=openapi.TYPE_STRING),
                'file_size': openapi.Schema(type=openapi.TYPE_INTEGER),
                'output_metadata': openapi.Schema(type=openapi.TYPE_OBJECT),
                'is_primary': openapi.Schema(type=openapi.TYPE_BOOLEAN),
            }
        ),
        responses={201: "Service output created"}
    )
    def post(self, request):
        """Create a new service output record"""
        try:
            # Get service execution
            execution_id = request.data.get('service_execution')
            execution = get_object_or_404(ServiceExecution, id=execution_id, user=request.user)

            # Create service output
            output = ServiceOutput.objects.create(
                service_execution=execution,
                output_name=request.data.get('output_name'),
                output_type=request.data.get('output_type', 'json'),
                file_extension=request.data.get('file_extension', ''),
                mime_type=request.data.get('mime_type', ''),
                file_size=request.data.get('file_size'),
                output_data=request.data.get('output_data'),
                output_text=request.data.get('output_text', ''),
                download_url=request.data.get('download_url', ''),
                preview_url=request.data.get('preview_url', ''),
                output_metadata=request.data.get('output_metadata', {}),
                is_primary=request.data.get('is_primary', False)
            )

            # Update execution output count
            execution.output_count = execution.outputs.count()
            execution.save()

            return Response({
                'id': output.id,
                'output_name': output.output_name,
                'output_type': output.output_type,
                'created_at': output.created_at
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error creating service output: {str(e)}")
            return Response(
                {'error': f'Failed to create service output: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )


# ═══════════════════════════════════════════════════════════════
# 💼 PE VALUE METRICS VIEWS - Checklists, Obligations, Velocity
# ═══════════════════════════════════════════════════════════════

class ClosingChecklistListCreateView(APIView):
    """
    List all closing checklist items for a deal or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List closing checklist items for a due diligence run",
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('category', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: ClosingChecklistSerializer(many=True)}
    )
    def get(self, request):
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({'error': 'dd_run_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = ClosingChecklist.objects.filter(
            client=request.user.client,
            due_diligence_run_id=dd_run_id
        )

        # Apply filters
        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        category = request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)

        serializer = ClosingChecklistSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new closing checklist item",
        request_body=ClosingChecklistCreateSerializer,
        responses={201: ClosingChecklistSerializer}
    )
    def post(self, request):
        serializer = ClosingChecklistCreateSerializer(data=clean_request_data_for_serializer(request.data, ClosingChecklistCreateSerializer), context={'request': request})
        if serializer.is_valid():
            checklist_item = serializer.save()
            return Response(
                ClosingChecklistSerializer(checklist_item).data,
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ClosingChecklistDetailView(APIView):
    """
    Retrieve, update or delete a closing checklist item.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get_object(self, pk, user):
        return get_object_or_404(ClosingChecklist, pk=pk, client=user.client, client__isnull=False)

    @swagger_auto_schema(
        operation_description="Retrieve a closing checklist item",
        responses={200: ClosingChecklistSerializer}
    )
    def get(self, request, pk):
        item = self.get_object(pk, request.user)
        serializer = ClosingChecklistSerializer(item)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Update a closing checklist item",
        request_body=ClosingChecklistCreateSerializer,
        responses={200: ClosingChecklistSerializer}
    )
    def patch(self, request, pk):
        item = self.get_object(pk, request.user)
        serializer = ClosingChecklistCreateSerializer(item, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(ClosingChecklistSerializer(item).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="Delete a closing checklist item",
        responses={204: 'No content'}
    )
    def delete(self, request, pk):
        item = self.get_object(pk, request.user)
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PostCloseObligationListCreateView(APIView):
    """
    List all post-close obligations for a deal or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List post-close obligations for a due diligence run",
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('obligation_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: PostCloseObligationSerializer(many=True)}
    )
    def get(self, request):
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({'error': 'dd_run_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = PostCloseObligation.objects.filter(
            client=request.user.client,
            due_diligence_run_id=dd_run_id
        )

        # Apply filters
        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        obligation_type = request.query_params.get('obligation_type')
        if obligation_type:
            queryset = queryset.filter(obligation_type=obligation_type)

        serializer = PostCloseObligationSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new post-close obligation",
        request_body=PostCloseObligationCreateSerializer,
        responses={201: PostCloseObligationSerializer}
    )
    def post(self, request):
        serializer = PostCloseObligationCreateSerializer(data=clean_request_data_for_serializer(request.data, PostCloseObligationCreateSerializer), context={'request': request})
        if serializer.is_valid():
            obligation = serializer.save()
            return Response(
                PostCloseObligationSerializer(obligation).data,
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DealVelocityMetricsListView(APIView):
    """
    List deal velocity metrics for a due diligence run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List deal velocity metrics for a due diligence run",
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
        ],
        responses={200: DealVelocityMetricsSerializer(many=True)}
    )
    def get(self, request):
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({'error': 'dd_run_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = DealVelocityMetrics.objects.filter(
            client=request.user.client,
            due_diligence_run_id=dd_run_id
        )

        serializer = DealVelocityMetricsSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create or update deal velocity metrics for a phase",
        request_body=DealVelocityMetricsSerializer,
        responses={201: DealVelocityMetricsSerializer}
    )
    def post(self, request):
        dd_run_id = request.data.get('due_diligence_run')
        phase = request.data.get('phase')

        # Check if metrics already exist for this phase
        existing = DealVelocityMetrics.objects.filter(
            client=request.user.client,
            due_diligence_run_id=dd_run_id,
            phase=phase
        ).first()

        if existing:
            serializer = DealVelocityMetricsSerializer(existing, data=request.data, partial=True)
        else:
            serializer = DealVelocityMetricsSerializer(data=request.data)

        if serializer.is_valid():
            if not existing:
                serializer.save(client=request.user.client)
            else:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ClauseLibraryListCreateView(APIView):
    """
    List all clauses in the library or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List clauses in the library",
        manual_parameters=[
            openapi.Parameter('category', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('risk_position', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: ClauseLibrarySerializer(many=True)}
    )
    def get(self, request):
        queryset = ClauseLibrary.objects.filter(
            client=request.user.client,
            is_active=True
        )

        # Apply filters
        category = request.query_params.get('category')
        if category:
            queryset = queryset.filter(clause_category=category)

        risk_position = request.query_params.get('risk_position')
        if risk_position:
            queryset = queryset.filter(risk_position=risk_position)

        search = request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(clause_name__icontains=search) |
                Q(clause_text__icontains=search) |
                Q(tags__contains=[search])
            )

        serializer = ClauseLibrarySerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new clause in the library",
        request_body=ClauseLibraryCreateSerializer,
        responses={201: ClauseLibrarySerializer}
    )
    def post(self, request):
        serializer = ClauseLibraryCreateSerializer(data=clean_request_data_for_serializer(request.data, ClauseLibraryCreateSerializer), context={'request': request})
        if serializer.is_valid():
            clause = serializer.save()
            return Response(
                ClauseLibrarySerializer(clause).data,
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DealVelocityAnalyticsView(APIView):
    """
    Get deal velocity analytics and bottleneck analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get deal velocity analytics summary",
        manual_parameters=[
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: DealVelocitySummarySerializer}
    )
    def get(self, request):
        start_time = time.time()
        client = request.user.client

        # Get all velocity metrics for the client
        metrics = DealVelocityMetrics.objects.filter(client=client)

        # Calculate summary statistics
        total_deals = metrics.values('due_diligence_run').distinct().count()

        # Calculate average phase durations
        phase_durations = metrics.values('phase').annotate(
            avg_duration=Avg('actual_duration_days')
        )
        avg_phase_duration = {
            item['phase']: item['avg_duration'] or 0
            for item in phase_durations
        }

        # Count bottlenecks
        total_bottlenecks = metrics.filter(is_bottleneck=True).count()
        resolved_bottlenecks = metrics.filter(is_bottleneck=True, bottleneck_resolved=True).count()

        # Count delayed deals
        delayed_phases = metrics.filter(
            actual_duration_days__gt=F('planned_duration_days')
        ).values('due_diligence_run').distinct().count()

        summary = {
            'total_deals': total_deals,
            'avg_deal_duration_days': sum(avg_phase_duration.values()),
            'deals_on_track': total_deals - delayed_phases,
            'deals_delayed': delayed_phases,
            'total_bottlenecks': total_bottlenecks,
            'resolved_bottlenecks': resolved_bottlenecks,
            'avg_phase_duration': avg_phase_duration
        }

        serializer = DealVelocitySummarySerializer(summary)
        response_data = serializer.data

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                from core.models import Run
                run = Run.objects.filter(user=request.user).order_by('-created_at').first()
                datetime_folder = request.query_params.get('datetime_folder')  # Use today if not provided
                report_info = generate_and_register_service_report(
                    service_name="PE Deal Velocity Analytics",
                    service_id="pe-deal-velocity",
                    vertical="Private Equity",
                    response_data=response_data,
                    user=request.user,
                    run=run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="deal-velocity-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"total_deals": total_deals, "deals_delayed": delayed_phases},
                    datetime_folder=datetime_folder
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate deal velocity report: {e}")

        return Response(response_data)


class ChecklistProgressView(APIView):
    """
    Get closing checklist progress for a deal.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get closing checklist progress for a deal",
        manual_parameters=[
            openapi.Parameter('dd_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
        ],
        responses={200: ChecklistProgressSerializer}
    )
    def get(self, request):
        dd_run_id = request.query_params.get('dd_run_id')
        if not dd_run_id:
            return Response({'error': 'dd_run_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        dd_run = get_object_or_404(DueDiligenceRun, pk=dd_run_id, client=request.user.client, client__isnull=False)
        items = ClosingChecklist.objects.filter(due_diligence_run=dd_run)

        total_items = items.count()
        completed_items = items.filter(status='completed').count()
        in_progress_items = items.filter(status='in_progress').count()
        blocked_items = items.filter(status='blocked').count()

        # Count overdue items
        from django.utils import timezone
        overdue_items = items.filter(
            due_date__lt=timezone.now().date()
        ).exclude(status__in=['completed', 'not_applicable']).count()

        # Group by category
        items_by_category = dict(
            items.values('category').annotate(count=Count('id')).values_list('category', 'count')
        )

        # Group by priority
        items_by_priority = dict(
            items.values('priority').annotate(count=Count('id')).values_list('priority', 'count')
        )

        progress = {
            'deal_name': dd_run.deal_name,
            'total_items': total_items,
            'completed_items': completed_items,
            'in_progress_items': in_progress_items,
            'blocked_items': blocked_items,
            'overdue_items': overdue_items,
            'completion_percentage': (completed_items / total_items * 100) if total_items > 0 else 0,
            'items_by_category': items_by_category,
            'items_by_priority': items_by_priority
        }

        serializer = ChecklistProgressSerializer(progress)
        return Response(serializer.data)


# ═══════════════════════════════════════════════════════════════
# 🏢 PANEL MANAGEMENT & RFP VIEWS
# ═══════════════════════════════════════════════════════════════

class PanelFirmListCreateView(APIView):
    """
    List all panel firms or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all panel firms",
        manual_parameters=[
            openapi.Parameter('practice_area', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('region', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: PanelFirmSerializer(many=True)}
    )
    def get(self, request):
        queryset = PanelFirm.objects.filter(client=request.user.client, client__isnull=False, is_active=True)

        # Apply filters
        practice_area = request.query_params.get('filter_practice_area')
        if practice_area:
            queryset = queryset.filter(practice_areas__contains=[practice_area])

        region = request.query_params.get('filter_region')
        if region:
            queryset = queryset.filter(regions__contains=[region])

        serializer = PanelFirmSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new panel firm",
        request_body=PanelFirmSerializer,
        responses={201: PanelFirmSerializer}
    )
    def post(self, request):
        serializer = PanelFirmSerializer(data=clean_request_data_for_serializer(request.data, PanelFirmSerializer), context={'request': request})
        if serializer.is_valid():
            firm = serializer.save()
            return Response(PanelFirmSerializer(firm).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RFPListCreateView(APIView):
    """
    List all RFPs or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all RFPs",
        manual_parameters=[
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False),
        ],
        responses={200: RFPSerializer(many=True)}
    )
    def get(self, request):
        queryset = RFP.objects.filter(client=request.user.client, client__isnull=False)

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if deal_workspace_id:
            queryset = queryset.filter(due_diligence_run_id=deal_workspace_id)

        serializer = RFPSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new RFP",
        request_body=RFPSerializer,
        responses={201: RFPSerializer}
    )
    def post(self, request):
        serializer = RFPSerializer(data=clean_request_data_for_serializer(request.data, RFPSerializer), context={'request': request})
        if serializer.is_valid():
            rfp = serializer.save()
            return Response(RFPSerializer(rfp).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BidAnalysisView(APIView):
    """
    Analyze and compare bids for an RFP.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Analyze bids for an RFP",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['rfp_id'],
            properties={
                'rfp_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'scoring_criteria': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_STRING)),
            }
        ),
        responses={200: BidAnalysisSerializer}
    )
    def post(self, request):
        rfp_id = request.data.get('rfp_id')
        if not rfp_id:
            return Response({'error': 'rfp_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        rfp = get_object_or_404(RFP, pk=rfp_id, client=request.user.client, client__isnull=False)
        bids = RFPBid.objects.filter(rfp=rfp)

        # Build comparison matrix
        bids_data = []
        for bid in bids:
            bids_data.append({
                'firm_name': bid.firm.name,
                'proposed_fee': float(bid.proposed_fee),
                'fee_structure': bid.fee_structure,
                'price_score': float(bid.price_score) if bid.price_score else 0,
                'experience_score': float(bid.experience_score) if bid.experience_score else 0,
                'team_score': float(bid.team_score) if bid.team_score else 0,
                'overall_score': float(bid.overall_score) if bid.overall_score else 0,
            })

        # Simple recommendation logic
        recommendation = {}
        if bids_data:
            best_bid = max(bids_data, key=lambda x: x['overall_score'], default=None)
            if best_bid:
                recommendation = {
                    'recommended_firm': best_bid['firm_name'],
                    'reason': 'Highest overall score',
                    'score': best_bid['overall_score']
                }

        analysis = {
            'rfp_id': rfp.id,
            'rfp_title': rfp.title,
            'bids': bids_data,
            'comparison_matrix': bids_data,
            'recommendation': recommendation
        }

        serializer = BidAnalysisSerializer(analysis)
        return Response(serializer.data)


class EngagementLetterListCreateView(APIView):
    """
    List all engagement letters or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all engagement letters",
        responses={200: EngagementLetterSerializer(many=True)}
    )
    def get(self, request):
        queryset = EngagementLetter.objects.filter(client=request.user.client, client__isnull=False)

        firm_id = request.query_params.get('firm_id')
        if firm_id:
            queryset = queryset.filter(firm_id=firm_id)

        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if deal_workspace_id:
            queryset = queryset.filter(due_diligence_run_id=deal_workspace_id)

        serializer = EngagementLetterSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new engagement letter",
        request_body=EngagementLetterSerializer,
        responses={201: EngagementLetterSerializer}
    )
    def post(self, request):
        serializer = EngagementLetterSerializer(data=clean_request_data_for_serializer(request.data, EngagementLetterSerializer), context={'request': request})
        if serializer.is_valid():
            letter = serializer.save()
            return Response(EngagementLetterSerializer(letter).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ═══════════════════════════════════════════════════════════════
# ✍️ SIGNATURE TRACKING VIEWS
# ═══════════════════════════════════════════════════════════════

class SignatureTrackerListCreateView(APIView):
    """
    List all signature trackers or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all signature trackers",
        manual_parameters=[
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: SignatureTrackerSerializer(many=True)}
    )
    def get(self, request):
        queryset = SignatureTracker.objects.filter(client=request.user.client, client__isnull=False)

        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if deal_workspace_id:
            queryset = queryset.filter(due_diligence_run_id=deal_workspace_id)

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        serializer = SignatureTrackerSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new signature tracker",
        request_body=SignatureTrackerSerializer,
        responses={201: SignatureTrackerSerializer}
    )
    def post(self, request):
        serializer = SignatureTrackerSerializer(data=clean_request_data_for_serializer(request.data, SignatureTrackerSerializer), context={'request': request})
        if serializer.is_valid():
            tracker = serializer.save()
            return Response(SignatureTrackerSerializer(tracker).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ═══════════════════════════════════════════════════════════════
# 📋 CLOSING MANAGEMENT VIEWS
# ═══════════════════════════════════════════════════════════════

class ConditionPrecedentListCreateView(APIView):
    """
    List all conditions precedent or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all conditions precedent",
        manual_parameters=[
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: ConditionPrecedentSerializer(many=True)}
    )
    def get(self, request):
        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if not deal_workspace_id:
            return Response({'error': 'deal_workspace_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = ConditionPrecedent.objects.filter(
            client=request.user.client,
            due_diligence_run_id=deal_workspace_id
        )

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        serializer = ConditionPrecedentSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new condition precedent",
        request_body=ConditionPrecedentSerializer,
        responses={201: ConditionPrecedentSerializer}
    )
    def post(self, request):
        serializer = ConditionPrecedentSerializer(data=clean_request_data_for_serializer(request.data, ConditionPrecedentSerializer), context={'request': request})
        if serializer.is_valid():
            cp = serializer.save()
            return Response(ConditionPrecedentSerializer(cp).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ClosingBinderListCreateView(APIView):
    """
    List all closing binders or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all closing binders",
        manual_parameters=[
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
        ],
        responses={200: ClosingBinderSerializer(many=True)}
    )
    def get(self, request):
        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if not deal_workspace_id:
            return Response({'error': 'deal_workspace_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = ClosingBinder.objects.filter(
            client=request.user.client,
            due_diligence_run_id=deal_workspace_id
        )

        serializer = ClosingBinderSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new closing binder",
        request_body=ClosingBinderSerializer,
        responses={201: ClosingBinderSerializer}
    )
    def post(self, request):
        serializer = ClosingBinderSerializer(data=clean_request_data_for_serializer(request.data, ClosingBinderSerializer), context={'request': request})
        if serializer.is_valid():
            binder = serializer.save()
            return Response(ClosingBinderSerializer(binder).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ═══════════════════════════════════════════════════════════════
# 📊 COMPLIANCE TRACKING VIEWS
# ═══════════════════════════════════════════════════════════════

class CovenantListCreateView(APIView):
    """
    List all covenants or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all covenants",
        manual_parameters=[
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: CovenantSerializer(many=True)}
    )
    def get(self, request):
        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if not deal_workspace_id:
            return Response({'error': 'deal_workspace_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = Covenant.objects.filter(
            client=request.user.client,
            due_diligence_run_id=deal_workspace_id
        )

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        serializer = CovenantSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new covenant",
        request_body=CovenantSerializer,
        responses={201: CovenantSerializer}
    )
    def post(self, request):
        serializer = CovenantSerializer(data=clean_request_data_for_serializer(request.data, CovenantSerializer), context={'request': request})
        if serializer.is_valid():
            covenant = serializer.save()
            return Response(CovenantSerializer(covenant).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ConsentFilingListCreateView(APIView):
    """
    List all consent filings or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="List all consent filings",
        manual_parameters=[
            openapi.Parameter('deal_workspace_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ],
        responses={200: ConsentFilingSerializer(many=True)}
    )
    def get(self, request):
        deal_workspace_id = request.query_params.get('deal_workspace_id')
        if not deal_workspace_id:
            return Response({'error': 'deal_workspace_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = ConsentFiling.objects.filter(
            client=request.user.client,
            due_diligence_run_id=deal_workspace_id
        )

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        serializer = ConsentFilingSerializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new consent filing",
        request_body=ConsentFilingSerializer,
        responses={201: ConsentFilingSerializer}
    )
    def post(self, request):
        serializer = ConsentFilingSerializer(data=clean_request_data_for_serializer(request.data, ConsentFilingSerializer), context={'request': request})
        if serializer.is_valid():
            filing = serializer.save()
            return Response(ConsentFilingSerializer(filing).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PortfolioComplianceView(APIView):
    """
    Get portfolio-wide compliance dashboard.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get portfolio-wide compliance dashboard",
        manual_parameters=[
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: PortfolioComplianceSerializer}
    )
    def get(self, request):
        start_time = time.time()
        client = request.user.client

        # Get all covenants for the client
        covenants = Covenant.objects.filter(client=client)
        total_covenants = covenants.count()
        compliant_covenants = covenants.filter(status='compliant').count()
        breached_covenants = covenants.filter(status='breached').count()
        at_risk_covenants = covenants.filter(status='at_risk').count()

        # Get all consent filings
        filings = ConsentFiling.objects.filter(client=client)
        total_filings = filings.count()
        completed_filings = filings.filter(status='approved').count()
        pending_filings = filings.filter(status='pending').count()

        # Get overdue items
        from django.utils import timezone
        overdue_covenants = covenants.filter(
            next_reporting_date__lt=timezone.now().date()
        ).exclude(status='compliant').count()

        overdue_filings = filings.filter(
            due_date__lt=timezone.now().date()
        ).exclude(status__in=['approved', 'withdrawn']).count()

        compliance_data = {
            'total_deals': DueDiligenceRun.objects.filter(client=client).count(),
            'total_obligations': total_covenants + total_filings,
            'pending_obligations': pending_filings + at_risk_covenants,
            'overdue_obligations': overdue_covenants + overdue_filings,
            'upcoming_deadlines': [],
            'risk_distribution': {
                'compliant': compliant_covenants,
                'at_risk': at_risk_covenants,
                'breached': breached_covenants,
            },
            'compliance_by_deal': [],
            # Covenant / filing detail (kept for existing consumers and reports)
            'total_covenants': total_covenants,
            'compliant_covenants': compliant_covenants,
            'breached_covenants': breached_covenants,
            'at_risk_covenants': at_risk_covenants,
            'covenant_compliance_rate': round(compliant_covenants / total_covenants * 100, 1) if total_covenants else 0.0,
            'total_filings': total_filings,
            'completed_filings': completed_filings,
            'pending_filings': pending_filings,
            'filing_completion_rate': round(completed_filings / total_filings * 100, 1) if total_filings else 0.0,
            'overdue_covenants': overdue_covenants,
            'overdue_filings': overdue_filings,
        }

        serializer = PortfolioComplianceSerializer(compliance_data)
        response_data = serializer.data

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                from core.models import Run
                run = Run.objects.filter(user=request.user).order_by('-created_at').first()
                datetime_folder = request.query_params.get('datetime_folder')
                report_info = generate_and_register_service_report(
                    service_name="PE Portfolio Compliance Dashboard",
                    service_id="pe-portfolio-compliance",
                    vertical="Private Equity",
                    response_data=response_data,
                    user=request.user,
                    run=run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="compliance-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"total_covenants": total_covenants, "breached_covenants": breached_covenants},
                    datetime_folder=datetime_folder
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate compliance report: {e}")

        return Response(response_data)


class RiskHeatmapView(APIView):
    """
    Get risk heatmap data for portfolio visualization.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get risk heatmap data for portfolio visualization",
        manual_parameters=[
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: RiskHeatmapSerializer}
    )
    def get(self, request):
        start_time = time.time()
        client = request.user.client

        # Get all DD runs for the client
        dd_runs = DueDiligenceRun.objects.filter(client=client)

        # Build heatmap data
        heatmap_data = []
        for dd_run in dd_runs:
            risk_clauses = RiskClause.objects.filter(due_diligence_run=dd_run)
            covenants = Covenant.objects.filter(due_diligence_run=dd_run)

            # Calculate risk score
            high_risk_count = risk_clauses.filter(risk_level='high').count()
            critical_risk_count = risk_clauses.filter(risk_level='critical').count()
            breached_covenants_count = covenants.filter(status='breached').count()

            risk_score = (high_risk_count * 2) + (critical_risk_count * 5) + (breached_covenants_count * 10)

            heatmap_data.append({
                'deal_id': dd_run.id,
                'deal_name': dd_run.deal_name,
                'target_company': dd_run.target_company,
                'risk_score': risk_score,
                'high_risk_clauses': high_risk_count,
                'critical_risk_clauses': critical_risk_count,
                'breached_covenants': breached_covenants_count,
                'risk_level': 'critical' if risk_score > 20 else 'high' if risk_score > 10 else 'medium' if risk_score > 5 else 'low'
            })

        # Sort by risk score descending
        heatmap_data.sort(key=lambda x: x['risk_score'], reverse=True)

        response_data = {
            'heatmap_type': 'portfolio_risk',
            'categories': [d['deal_name'] for d in heatmap_data],
            'data': heatmap_data,
            'summary': {
                'total_deals': len(heatmap_data),
                'high_risk_deals': len([d for d in heatmap_data if d['risk_level'] in ['high', 'critical']]),
            },
            # Flat fields (kept for existing consumers and reports)
            'deals': heatmap_data,
            'total_deals': len(heatmap_data),
            'high_risk_deals': len([d for d in heatmap_data if d['risk_level'] in ['high', 'critical']]),
        }

        serializer = RiskHeatmapSerializer(response_data)
        final_response = serializer.data

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                from core.models import Run
                run = Run.objects.filter(user=request.user).order_by('-created_at').first()
                datetime_folder = request.query_params.get('datetime_folder')
                report_info = generate_and_register_service_report(
                    service_name="PE Risk Heatmap Analysis",
                    service_id="pe-risk-heatmap",
                    vertical="Private Equity",
                    response_data=response_data,
                    user=request.user,
                    run=run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="risk-heatmap-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"total_deals": response_data['summary']['total_deals'], "high_risk_deals": response_data['summary']['high_risk_deals']},
                    datetime_folder=datetime_folder
                )
                final_response['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate risk heatmap report: {e}")

        return Response(final_response)

    def post(self, request):
        return self.get(request)


# ═══════════════════════════════════════════════════════════════
# 📊 PE TASK STATUS VIEW - Celery Task Status with registered_outputs
# ═══════════════════════════════════════════════════════════════

class PETaskStatusView(APIView):
    """
    Check the status of Private Equity Celery tasks and return registered_outputs.
    Similar to TranslationTaskStatusView pattern.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get the status of a PE task by task_id",
        tags=["Private Equity - Task Status"],
        manual_parameters=[
            openapi.Parameter('task_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
                            description='Celery task ID'),
            openapi.Parameter('task_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
                            description='Task type: classify, extract_risk, findings_report, sync_data_room'),
        ],
        responses={
            200: openapi.Response(
                description="Task status with registered_outputs",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'task_id': openapi.Schema(type=openapi.TYPE_STRING),
                        'status': openapi.Schema(type=openapi.TYPE_STRING),
                        'result': openapi.Schema(type=openapi.TYPE_OBJECT),
                        'registered_outputs': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'filename': openapi.Schema(type=openapi.TYPE_STRING),
                                    'file_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'path': openapi.Schema(type=openapi.TYPE_STRING),
                                }
                            )
                        ),
                    }
                )
            )
        }
    )
    def get(self, request):
        """Get the status of a PE task"""
        from celery.result import AsyncResult

        task_id = request.query_params.get('task_id')
        if not task_id:
            return Response(
                {"error": "task_id parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            result = AsyncResult(task_id)
            task_status = result.status

            # Ensure status is a plain string (Celery may return exception objects)
            if not isinstance(task_status, str):
                task_status = str(task_status)

            task_result = result.result if result.ready() else None

            # Ensure result is JSON-serializable
            if task_result is not None and not isinstance(task_result, (dict, list, str, int, float, bool, type(None))):
                task_result = str(task_result)

            response_data = {
                'task_id': task_id,
                'status': task_status,
                'result': task_result,
                'registered_outputs': []
            }

            # If task completed successfully, extract registered_outputs
            if task_status == 'SUCCESS' and task_result:
                # Check if result already has registered_outputs
                if isinstance(task_result, dict) and 'registered_outputs' in task_result:
                    response_data['registered_outputs'] = task_result['registered_outputs']
                # For findings report, get the report file
                elif isinstance(task_result, dict) and 'report_id' in task_result:
                    report = FindingsReport.objects.filter(
                        id=task_result['report_id'],
                        user=request.user
                    ).first()
                    if report and report.report_file:
                        response_data['registered_outputs'] = [{
                            'filename': report.report_file.filename,
                            'file_id': report.report_file.id,
                            'path': get_logical_path(report.report_file.filepath, report.report_file.filename)
                        }]
                # For classification tasks, get the classification record
                elif isinstance(task_result, dict) and 'file_id' in task_result:
                    file_obj = File.objects.filter(
                        id=task_result['file_id'],
                        user=request.user
                    ).first()
                    if file_obj:
                        response_data['registered_outputs'] = [{
                            'filename': file_obj.filename,
                            'file_id': file_obj.id,
                            'path': file_obj.filepath
                        }]

            elif task_status == 'FAILURE':
                response_data['error'] = str(task_result) if task_result else 'Task failed'

            elif task_status == 'PENDING':
                response_data['message'] = 'Task is pending or does not exist'

            return Response(response_data)

        except Exception as e:
            logger.error(f"Error checking task status for {task_id}: {str(e)}")
            return Response(
                {"error": f"Failed to check task status: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
