import time
import logging

from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Avg
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.parsers import MultiPartParser, FormParser
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from core.utils import generate_and_register_service_report, clean_request_data_for_serializer, get_user_client

from custom_authentication.permissions import IsClientOrAdmin, IsClientOrAdminOrSuperUser
from core.models import File
from .models import (
    MassClaimsRun, IntakeForm, EvidenceDocument, PIIRedaction,
    ExhibitPackage, SettlementTracking, ClaimantCommunication,
    ServiceExecution, ServiceOutput
)
from .serializers import (
    MassClaimsRunSerializer, MassClaimsRunCreateSerializer,
    IntakeFormSerializer, IntakeFormCreateSerializer,
    EvidenceDocumentSerializer, PIIRedactionSerializer,
    ExhibitPackageSerializer, SettlementTrackingSerializer,
    ClaimantCommunicationSerializer, EvidenceSummarySerializer,
    IntakeFormSummarySerializer
)
from .tasks import (
    process_intake_form_task, cull_evidence_documents_task,
    redact_pii_task, generate_exhibit_package_task,
    detect_duplicate_claims_task
)

logger = logging.getLogger(__name__)


class MassClaimsRunListCreateView(APIView):
    """
    List all mass claims runs for the authenticated user or create a new one.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="List all mass claims runs for the authenticated user",
        tags=["Class Actions - Mass Claims"],
        responses={200: MassClaimsRunSerializer(many=True)}
    )
    def get(self, request):
        """List all mass claims runs for the user"""
        runs = MassClaimsRun.objects.filter(run__user=request.user)
        serializer = MassClaimsRunSerializer(runs, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Create a new mass claims run",
        tags=["Class Actions - Mass Claims"],
        request_body=MassClaimsRunCreateSerializer,
        responses={201: MassClaimsRunSerializer}
    )
    def post(self, request):
        """Create a new mass claims run"""
        serializer = MassClaimsRunCreateSerializer(data=clean_request_data_for_serializer(request.data, MassClaimsRunCreateSerializer), context={'request': request})
        if serializer.is_valid():
            mass_claims_run = serializer.save()
            response_serializer = MassClaimsRunSerializer(mass_claims_run)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MassClaimsRunDetailView(APIView):
    """
    Retrieve, update or delete a specific mass claims run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    def get_object(self, pk, user):
        """Get mass claims run ensuring user ownership"""
        return get_object_or_404(MassClaimsRun, pk=pk, run__user=user)
    
    @swagger_auto_schema(
        operation_description="Retrieve a specific mass claims run",
        tags=["Class Actions - Mass Claims"],
        responses={200: MassClaimsRunSerializer}
    )
    def get(self, request, pk):
        """Retrieve a specific mass claims run"""
        mc_run = self.get_object(pk, request.user)
        serializer = MassClaimsRunSerializer(mc_run)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Update a specific mass claims run",
        tags=["Class Actions - Mass Claims"],
        request_body=MassClaimsRunSerializer,
        responses={200: MassClaimsRunSerializer}
    )
    def put(self, request, pk):
        """Update a specific mass claims run"""
        mc_run = self.get_object(pk, request.user)
        serializer = MassClaimsRunSerializer(mc_run, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @swagger_auto_schema(
        operation_description="Delete a specific mass claims run",
        tags=["Class Actions - Mass Claims"],
        responses={204: "No Content"}
    )
    def delete(self, request, pk):
        """Delete a specific mass claims run"""
        mc_run = self.get_object(pk, request.user)
        mc_run.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IntakeFormView(APIView):
    """
    Manage intake forms for mass claims.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get intake forms for a mass claims run",
        tags=["Class Actions - Intake Forms"],
        manual_parameters=[
            openapi.Parameter('mc_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: IntakeFormSerializer(many=True)}
    )
    def get(self, request):
        """Get intake forms for a mass claims run with optional filtering"""
        mc_run_id = request.query_params.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)
        
        intake_forms = IntakeForm.objects.filter(
            mass_claims_run=mc_run,
            user=request.user
        )
        
        # Apply status filter
        status_filter = request.query_params.get('status')
        if status_filter:
            intake_forms = intake_forms.filter(processing_status=status_filter)
        
        serializer = IntakeFormSerializer(intake_forms, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Submit a new intake form",
        tags=["Class Actions - Intake Forms"],
        request_body=IntakeFormCreateSerializer,
        responses={201: IntakeFormSerializer}
    )
    def post(self, request):
        """Submit a new intake form"""
        serializer = IntakeFormCreateSerializer(data=clean_request_data_for_serializer(request.data, IntakeFormCreateSerializer), context={'request': request})
        if serializer.is_valid():
            intake_form = serializer.save()
            
            # Trigger processing task
            process_intake_form_task.delay(intake_form.id, request.user.id)
            
            response_serializer = IntakeFormSerializer(intake_form)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class EvidenceDocumentView(APIView):
    """
    Manage evidence documents with culling and relevance scoring.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get evidence documents for a mass claims run",
        tags=["Class Actions - Evidence Management"],
        manual_parameters=[
            openapi.Parameter('mc_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('evidence_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('is_culled', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False)
        ],
        responses={200: EvidenceDocumentSerializer(many=True)}
    )
    def get(self, request):
        """Get evidence documents for a mass claims run with optional filtering"""
        mc_run_id = request.query_params.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)
        
        evidence_docs = EvidenceDocument.objects.filter(
            mass_claims_run=mc_run,
            user=request.user
        )
        
        # Apply filters
        evidence_type = request.query_params.get('evidence_type')
        if evidence_type:
            evidence_docs = evidence_docs.filter(evidence_type=evidence_type)
        
        is_culled = request.query_params.get('is_culled')
        if is_culled is not None:
            is_culled_bool = is_culled.lower() == 'true'
            evidence_docs = evidence_docs.filter(is_culled=is_culled_bool)
        
        serializer = EvidenceDocumentSerializer(evidence_docs, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Trigger evidence culling for documents",
        tags=["Class Actions - Evidence Management"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'mc_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'file_ids': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_INTEGER))
            },
            required=['mc_run_id', 'file_ids']
        ),
        responses={202: "Evidence culling started"}
    )
    def post(self, request):
        """Trigger evidence culling for documents"""
        mc_run_id = request.data.get('mc_run_id')
        file_ids = request.data.get('file_ids', [])
        
        if not mc_run_id or not file_ids:
            return Response(
                {"error": "mc_run_id and file_ids are required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)
        
        # Ensure user owns all files
        files = File.objects.filter(id__in=file_ids, user=request.user)
        if files.count() != len(file_ids):
            return Response(
                {"error": "Some files not found or not owned by user"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Trigger culling task
        task = cull_evidence_documents_task.delay(mc_run.id, file_ids, request.user.id)
        
        return Response({
            "message": "Evidence culling started",
            "task_id": task.id,
            "files_count": len(file_ids)
        }, status=status.HTTP_202_ACCEPTED)


class PIIRedactionView(APIView):
    """
    Manage PII redaction for documents.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]
    
    @swagger_auto_schema(
        operation_description="Get PII redactions for a mass claims run",
        tags=["Class Actions - PII Redaction"],
        manual_parameters=[
            openapi.Parameter('mc_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('pii_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: PIIRedactionSerializer(many=True)}
    )
    def get(self, request):
        """Get PII redactions for a mass claims run"""
        mc_run_id = request.query_params.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)
        
        redactions = PIIRedaction.objects.filter(
            mass_claims_run=mc_run,
            user=request.user
        )
        
        # Apply PII type filter
        pii_type = request.query_params.get('pii_type')
        if pii_type:
            redactions = redactions.filter(pii_type=pii_type)
        
        serializer = PIIRedactionSerializer(redactions, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description="Trigger PII redaction for documents",
        tags=["Class Actions - PII Redaction"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'mc_run_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'file_ids': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_INTEGER))
            },
            required=['mc_run_id', 'file_ids']
        ),
        responses={202: "PII redaction started"}
    )
    def post(self, request):
        """Trigger PII redaction for documents"""
        mc_run_id = request.data.get('mc_run_id')
        file_ids = request.data.get('file_ids', [])
        
        if not mc_run_id or not file_ids:
            return Response(
                {"error": "mc_run_id and file_ids are required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)
        
        # Ensure user owns all files
        files = File.objects.filter(id__in=file_ids, user=request.user)
        if files.count() != len(file_ids):
            return Response(
                {"error": "Some files not found or not owned by user"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Trigger PII redaction task
        task = redact_pii_task.delay(mc_run.id, file_ids, request.user.id)
        
        return Response({
            "message": "PII redaction started",
            "task_id": task.id,
            "files_count": len(file_ids)
        }, status=status.HTTP_202_ACCEPTED)


class ExhibitPackageView(APIView):
    """
    Manage exhibit packages with Bates stamping.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get exhibit packages for a mass claims run",
        tags=["Class Actions - Exhibit Management"],
        manual_parameters=[
            openapi.Parameter('mc_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: ExhibitPackageSerializer(many=True)}
    )
    def get(self, request):
        """Get exhibit packages for a mass claims run"""
        mc_run_id = request.query_params.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)

        packages = ExhibitPackage.objects.filter(
            mass_claims_run=mc_run,
            user=request.user
        )
        serializer = ExhibitPackageSerializer(packages, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new exhibit package",
        tags=["Class Actions - Exhibit Management"],
        request_body=ExhibitPackageSerializer,
        responses={201: ExhibitPackageSerializer}
    )
    def post(self, request):
        """Create a new exhibit package"""
        serializer = ExhibitPackageSerializer(data=clean_request_data_for_serializer(request.data, ExhibitPackageSerializer))
        if serializer.is_valid():
            # Ensure user owns the mass claims run
            mc_run = get_object_or_404(MassClaimsRun, pk=request.data.get('mass_claims_run'), run__user=request.user)

            package = serializer.save(user=request.user)

            # Trigger package generation task if files are provided
            if package.files.exists():
                generate_exhibit_package_task.delay(package.id, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SettlementTrackingView(APIView):
    """
    Manage settlement tracking and distribution.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get settlement tracking for a mass claims run",
        tags=["Class Actions - Settlement"],
        manual_parameters=[
            openapi.Parameter('mc_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: SettlementTrackingSerializer(many=True)}
    )
    def get(self, request):
        """Get settlement tracking for a mass claims run"""
        mc_run_id = request.query_params.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)

        settlements = SettlementTracking.objects.filter(
            mass_claims_run=mc_run,
            user=request.user
        )
        serializer = SettlementTrackingSerializer(settlements, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create or update settlement tracking",
        tags=["Class Actions - Settlement"],
        request_body=SettlementTrackingSerializer,
        responses={201: SettlementTrackingSerializer}
    )
    def post(self, request):
        """Create or update settlement tracking"""
        serializer = SettlementTrackingSerializer(data=clean_request_data_for_serializer(request.data, SettlementTrackingSerializer))
        if serializer.is_valid():
            # Ensure user owns the mass claims run
            mc_run = get_object_or_404(MassClaimsRun, pk=request.data.get('mass_claims_run'), run__user=request.user)

            settlement = serializer.save(user=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DuplicateDetectionView(APIView):
    """
    Detect duplicate intake forms.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Trigger duplicate detection for intake forms",
        tags=["Class Actions - Duplicate Detection"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'mc_run_id': openapi.Schema(type=openapi.TYPE_INTEGER)
            },
            required=['mc_run_id']
        ),
        responses={202: "Duplicate detection started"}
    )
    def post(self, request):
        """Trigger duplicate detection for intake forms"""
        mc_run_id = request.data.get('mc_run_id')

        if not mc_run_id:
            return Response(
                {"error": "mc_run_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)

        # Trigger duplicate detection task
        task = detect_duplicate_claims_task.delay(mc_run.id, request.user.id)

        return Response({
            "message": "Duplicate detection started",
            "task_id": task.id,
            "mc_run_id": mc_run_id
        }, status=status.HTTP_202_ACCEPTED)


class ExtractDamagesView(APIView):
    """
    POST /api/v1/class-actions/extract-damages/
    Trigger damages extraction for a mass claims run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def post(self, request):
        mc_run_id = request.data.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)
        return Response({
            "message": "Damages extraction started",
            "mc_run_id": mc_run_id
        }, status=status.HTTP_202_ACCEPTED)


class IssueTaggingView(APIView):
    """
    POST /api/v1/class-actions/issue-tagging/
    Trigger issue tagging for a mass claims run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def post(self, request):
        mc_run_id = request.data.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)
        return Response({
            "message": "Issue tagging started",
            "mc_run_id": mc_run_id
        }, status=status.HTTP_202_ACCEPTED)


class EvidenceSummaryView(APIView):
    """
    Get summary statistics for evidence documents.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get evidence document summary statistics",
        tags=["Class Actions - Analytics"],
        manual_parameters=[
            openapi.Parameter('mc_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: EvidenceSummarySerializer(many=True)}
    )
    def get(self, request):
        """Get evidence document summary statistics"""
        start_time = time.time()
        mc_run_id = request.query_params.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)

        # Get summary statistics grouped by evidence type
        summary_data = []
        evidence_types = EvidenceDocument.objects.filter(
            mass_claims_run=mc_run,
            user=request.user
        ).values_list('evidence_type', flat=True).distinct()

        for evidence_type in evidence_types:
            docs = EvidenceDocument.objects.filter(
                mass_claims_run=mc_run,
                user=request.user,
                evidence_type=evidence_type
            )

            summary_data.append({
                'evidence_type': evidence_type,
                'evidence_type_display': dict(EvidenceDocument._meta.get_field('evidence_type').choices)[evidence_type],
                'total_count': docs.count(),
                'culled_count': docs.filter(is_culled=True).count(),
                'pii_count': docs.filter(contains_pii=True).count(),
                'privileged_count': docs.exclude(privilege_status='none').count(),
                'avg_relevance_score': docs.aggregate(Avg('relevance_score'))['relevance_score__avg'] or 0.0,
            })

        serializer = EvidenceSummarySerializer(summary_data, many=True)
        response_data = {'summary': serializer.data, 'mc_run_id': mc_run_id, 'case_name': mc_run.case_name}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="Class Actions Evidence Summary",
                    service_id="ca-evidence-summary",
                    vertical="Class Actions",
                    response_data=response_data,
                    user=request.user,
                    run=mc_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="evidence-summary-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"case_name": mc_run.case_name, "evidence_types_count": len(summary_data)}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate evidence summary report: {e}")

        return Response(response_data)


class IntakeFormSummaryView(APIView):
    """
    Get summary statistics for intake forms.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get intake form summary statistics",
        tags=["Class Actions - Analytics"],
        manual_parameters=[
            openapi.Parameter('mc_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: IntakeFormSummarySerializer(many=True)}
    )
    def get(self, request):
        """Get intake form summary statistics"""
        start_time = time.time()
        mc_run_id = request.query_params.get('mc_run_id')
        if not mc_run_id:
            return Response({"error": "mc_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the mass claims run
        mc_run = get_object_or_404(MassClaimsRun, pk=mc_run_id, run__user=request.user)

        # Get summary statistics grouped by processing status
        summary_data = []
        statuses = IntakeForm.objects.filter(
            mass_claims_run=mc_run,
            user=request.user
        ).values_list('processing_status', flat=True).distinct()

        for status_val in statuses:
            forms = IntakeForm.objects.filter(
                mass_claims_run=mc_run,
                user=request.user,
                processing_status=status_val
            )

            summary_data.append({
                'processing_status': status_val,
                'processing_status_display': dict(IntakeForm._meta.get_field('processing_status').choices)[status_val],
                'total_count': forms.count(),
                'duplicate_count': forms.filter(is_duplicate=True).count(),
                'valid_count': forms.filter(is_valid=True).count(),
            })

        serializer = IntakeFormSummarySerializer(summary_data, many=True)
        response_data = {'summary': serializer.data, 'mc_run_id': mc_run_id, 'case_name': mc_run.case_name}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="Class Actions Intake Form Summary",
                    service_id="ca-intake-summary",
                    vertical="Class Actions",
                    response_data=response_data,
                    user=request.user,
                    run=mc_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="intake-summary-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"case_name": mc_run.case_name, "status_count": len(summary_data)}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate intake summary report: {e}")

        return Response(response_data)


class ServiceExecutionListCreateView(APIView):
    """
    API view for listing and creating service executions for Class Actions.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        """List service executions with optional filtering."""
        try:
            # Get query parameters
            mass_claims_run_id = request.query_params.get('mass_claims_run_id')
            service_type = request.query_params.get('service_type')
            status = request.query_params.get('status')

            # Base queryset
            queryset = ServiceExecution.objects.filter(user=request.user)

            # Apply filters
            if mass_claims_run_id:
                queryset = queryset.filter(mass_claims_run_id=mass_claims_run_id)
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
                    'mass_claims_run_id': execution.mass_claims_run.id if execution.mass_claims_run else None,
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

            # Get mass claims run if provided
            mass_claims_run = None
            if 'mass_claims_run_id' in data:
                try:
                    mass_claims_run = MassClaimsRun.objects.get(
                        id=data['mass_claims_run_id'],
                        user=request.user
                    )
                except MassClaimsRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': 'Mass claims run not found'
                    }, status=status.HTTP_404_NOT_FOUND)

            # Create service execution
            execution = ServiceExecution.objects.create(
                user=request.user,
                client=get_user_client(request.user),
                mass_claims_run=mass_claims_run,
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
    API view for listing and creating service outputs for Class Actions.
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
