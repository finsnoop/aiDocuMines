import time
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.generics import get_object_or_404
from django.db.models import Avg, Count, Q
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication

from core.utils import generate_and_register_service_report, clean_request_data_for_serializer, get_user_client

logger = logging.getLogger(__name__)

from custom_authentication.permissions import IsClientOrAdmin
from .models import (
    PatentAnalysisRun, PatentDocument, PatentClaim, PriorArtDocument,
    ClaimChart, PatentLandscape, InfringementAnalysis, ValidityChallenge,
    ServiceExecution, ServiceOutput
)
from .serializers import (
    PatentAnalysisRunSerializer, PatentAnalysisRunCreateSerializer,
    PatentDocumentSerializer, PatentClaimSerializer,
    PriorArtDocumentSerializer, ClaimChartSerializer, PatentLandscapeSerializer,
    InfringementAnalysisSerializer, ValidityChallengeSerializer,
    PatentAnalysisSummarySerializer, ClaimChartSummarySerializer,
    InfringementSummarySerializer, ValiditySummarySerializer
)
from .tasks import (
    extract_patent_data_task, analyze_patent_claims_task, search_prior_art_task,
    generate_claim_chart_task, analyze_infringement_task, analyze_validity_task
)


class PatentAnalysisRunListCreateView(APIView):
    """
    List and create patent analysis runs for IP litigation.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get list of patent analysis runs",
        tags=["IP Litigation - Analysis Runs"],
        responses={200: PatentAnalysisRunSerializer(many=True)}
    )
    def get(self, request):
        """Get list of patent analysis runs for the client (multi-tenant)"""
        # Filter by client for multi-tenant isolation
        # Fall back to user filter if client is not available
        if hasattr(request.user, 'client') and request.user.client:
            runs = PatentAnalysisRun.objects.filter(client=request.user.client, client__isnull=False)
        else:
            runs = PatentAnalysisRun.objects.filter(run__user=request.user)
        serializer = PatentAnalysisRunSerializer(runs, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new patent analysis run",
        tags=["IP Litigation - Analysis Runs"],
        request_body=PatentAnalysisRunCreateSerializer,
        responses={201: PatentAnalysisRunSerializer}
    )
    def post(self, request):
        """Create a new patent analysis run"""
        serializer = PatentAnalysisRunCreateSerializer(data=clean_request_data_for_serializer(request.data, PatentAnalysisRunCreateSerializer), context={'request': request})
        if serializer.is_valid():
            analysis_run = serializer.save()
            response_serializer = PatentAnalysisRunSerializer(analysis_run)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PatentAnalysisRunDetailView(APIView):
    """
    Retrieve, update, and delete patent analysis runs.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get patent analysis run details",
        tags=["IP Litigation - Analysis Runs"],
        responses={200: PatentAnalysisRunSerializer}
    )
    def get(self, request, pk):
        """Get patent analysis run details"""
        # Filter by client for multi-tenant isolation
        if hasattr(request.user, 'client') and request.user.client:
            analysis_run = get_object_or_404(PatentAnalysisRun, pk=pk, client=request.user.client, client__isnull=False)
        else:
            analysis_run = get_object_or_404(PatentAnalysisRun, pk=pk, run__user=request.user)
        serializer = PatentAnalysisRunSerializer(analysis_run)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Update patent analysis run",
        tags=["IP Litigation - Analysis Runs"],
        request_body=PatentAnalysisRunSerializer,
        responses={200: PatentAnalysisRunSerializer}
    )
    def put(self, request, pk):
        """Update patent analysis run"""
        # Filter by client for multi-tenant isolation
        if hasattr(request.user, 'client') and request.user.client:
            analysis_run = get_object_or_404(PatentAnalysisRun, pk=pk, client=request.user.client, client__isnull=False)
        else:
            analysis_run = get_object_or_404(PatentAnalysisRun, pk=pk, run__user=request.user)
        serializer = PatentAnalysisRunSerializer(analysis_run, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="Delete patent analysis run",
        tags=["IP Litigation - Analysis Runs"],
        responses={204: "Patent analysis run deleted"}
    )
    def delete(self, request, pk):
        """Delete patent analysis run"""
        # Filter by client for multi-tenant isolation
        if hasattr(request.user, 'client') and request.user.client:
            analysis_run = get_object_or_404(PatentAnalysisRun, pk=pk, client=request.user.client, client__isnull=False)
        else:
            analysis_run = get_object_or_404(PatentAnalysisRun, pk=pk, run__user=request.user)
        analysis_run.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PatentDocumentView(APIView):
    """
    Manage patent documents for analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get patent documents for an analysis run",
        tags=["IP Litigation - Patent Documents"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: PatentDocumentSerializer(many=True)}
    )
    def get(self, request):
        """Get patent documents for an analysis run"""
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)
        documents = PatentDocument.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        )

        serializer = PatentDocumentSerializer(documents, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Add a patent document to analysis",
        tags=["IP Litigation - Patent Documents"],
        request_body=PatentDocumentSerializer,
        responses={201: PatentDocumentSerializer}
    )
    def post(self, request):
        """Add a patent document to analysis"""
        serializer = PatentDocumentSerializer(data=clean_request_data_for_serializer(request.data, PatentDocumentSerializer), context={'request': request})
        if serializer.is_valid():
            document = serializer.save()

            # Trigger patent data extraction task
            extract_patent_data_task.delay(document.id, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)



class PatentClaimView(APIView):
    """
    Manage patent claims extracted from patent documents.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get patent claims for a patent document",
        tags=["IP Litigation - Patent Claims"],
        manual_parameters=[
            openapi.Parameter('patent_document_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: PatentClaimSerializer(many=True)}
    )
    def get(self, request):
        """Get patent claims for a patent document"""
        patent_document_id = request.query_params.get('patent_document_id')
        if not patent_document_id:
            return Response({"error": "patent_document_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the patent document
        patent_document = get_object_or_404(PatentDocument, pk=patent_document_id, user=request.user)
        claims = PatentClaim.objects.filter(
            patent_document=patent_document,
            user=request.user
        )

        serializer = PatentClaimSerializer(claims, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Trigger patent claim analysis",
        tags=["IP Litigation - Patent Claims"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'patent_document_id': openapi.Schema(type=openapi.TYPE_INTEGER)
            },
            required=['patent_document_id']
        ),
        responses={202: "Patent claim analysis started"}
    )
    def post(self, request):
        """Trigger patent claim analysis"""
        patent_document_id = request.data.get('patent_document_id')

        if not patent_document_id:
            return Response(
                {"error": "patent_document_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure user owns the patent document
        patent_document = get_object_or_404(PatentDocument, pk=patent_document_id, user=request.user)

        # Trigger claim analysis task
        task = analyze_patent_claims_task.delay(patent_document.id, request.user.id)

        return Response({
            "message": "Patent claim analysis started",
            "task_id": task.id,
            "patent_document_id": patent_document_id
        }, status=status.HTTP_202_ACCEPTED)


class PriorArtDocumentView(APIView):
    """
    Manage prior art documents for patent analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get prior art documents for an analysis run",
        tags=["IP Litigation - Prior Art"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('document_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: PriorArtDocumentSerializer(many=True)}
    )
    def get(self, request):
        """Get prior art documents for an analysis run"""
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)
        documents = PriorArtDocument.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        )

        # Apply document type filter
        document_type = request.query_params.get('document_type')
        if document_type:
            documents = documents.filter(document_type=document_type)

        serializer = PriorArtDocumentSerializer(documents, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Add a prior art document",
        tags=["IP Litigation - Prior Art"],
        request_body=PriorArtDocumentSerializer,
        responses={201: PriorArtDocumentSerializer}
    )
    def post(self, request):
        """Add a prior art document"""
        serializer = PriorArtDocumentSerializer(data=clean_request_data_for_serializer(request.data, PriorArtDocumentSerializer), context={'request': request})
        if serializer.is_valid():
            document = serializer.save()

            # Trigger prior art analysis task
            search_prior_art_task.delay(document.id, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ClaimChartView(APIView):
    """
    Manage claim charts for infringement and invalidity analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get claim charts for an analysis run",
        tags=["IP Litigation - Claim Charts"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('chart_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
        ],
        responses={200: ClaimChartSerializer(many=True)}
    )
    def get(self, request):
        """Get claim charts for an analysis run"""
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)
        charts = ClaimChart.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        )

        # Apply chart type filter
        chart_type = request.query_params.get('chart_type')
        if chart_type:
            charts = charts.filter(chart_type=chart_type)

        serializer = ClaimChartSerializer(charts, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new claim chart",
        tags=["IP Litigation - Claim Charts"],
        request_body=ClaimChartSerializer,
        responses={201: ClaimChartSerializer}
    )
    def post(self, request):
        """Create a new claim chart"""
        serializer = ClaimChartSerializer(data=clean_request_data_for_serializer(request.data, ClaimChartSerializer), context={'request': request})
        if serializer.is_valid():
            chart = serializer.save()

            # Trigger claim chart generation task
            generate_claim_chart_task.delay(chart.id, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ClaimChartDetailView(APIView):
    """
    Retrieve, update, and delete claim charts.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get claim chart details",
        tags=["IP Litigation - Claim Charts"],
        responses={200: ClaimChartSerializer}
    )
    def get(self, request, pk):
        """Get claim chart details"""
        chart = get_object_or_404(ClaimChart, pk=pk, user=request.user)
        serializer = ClaimChartSerializer(chart)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Update claim chart",
        tags=["IP Litigation - Claim Charts"],
        request_body=ClaimChartSerializer,
        responses={200: ClaimChartSerializer}
    )
    def put(self, request, pk):
        """Update claim chart"""
        chart = get_object_or_404(ClaimChart, pk=pk, user=request.user)
        serializer = ClaimChartSerializer(chart, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="Delete claim chart",
        tags=["IP Litigation - Claim Charts"],
        responses={204: "Claim chart deleted"}
    )
    def delete(self, request, pk):
        """Delete claim chart"""
        chart = get_object_or_404(ClaimChart, pk=pk, user=request.user)
        chart.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class InfringementAnalysisView(APIView):
    """
    Manage infringement analyses for patent litigation.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get infringement analyses for an analysis run",
        tags=["IP Litigation - Infringement Analysis"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: InfringementAnalysisSerializer(many=True)}
    )
    def get(self, request):
        """Get infringement analyses for an analysis run"""
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)
        analyses = InfringementAnalysis.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        )

        serializer = InfringementAnalysisSerializer(analyses, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Create a new infringement analysis",
        tags=["IP Litigation - Infringement Analysis"],
        request_body=InfringementAnalysisSerializer,
        responses={201: InfringementAnalysisSerializer}
    )
    def post(self, request):
        """Create a new infringement analysis"""
        serializer = InfringementAnalysisSerializer(data=clean_request_data_for_serializer(request.data, InfringementAnalysisSerializer), context={'request': request})
        if serializer.is_valid():
            analysis = serializer.save()

            # Trigger infringement analysis task
            analyze_infringement_task.delay(analysis.id, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ValidityChallengeView(APIView):
    """
    Manage patent validity challenges.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get validity challenges for an analysis run",
        tags=["IP Litigation - Validity Challenges"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ],
        responses={200: ValidityChallengeSerializer(many=True)}
    )
    def get(self, request):
        """Get validity challenges for an analysis run"""
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)
        challenges = ValidityChallenge.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        )

        serializer = ValidityChallengeSerializer(challenges, many=True)
        return Response(serializer.data)


    @swagger_auto_schema(
        operation_description="Create a new validity challenge",
        tags=["IP Litigation - Validity Challenges"],
        request_body=ValidityChallengeSerializer,
        responses={201: ValidityChallengeSerializer}
    )
    def post(self, request):
        """Create a new validity challenge"""
        serializer = ValidityChallengeSerializer(data=clean_request_data_for_serializer(request.data, ValidityChallengeSerializer), context={'request': request})
        if serializer.is_valid():
            challenge = serializer.save()

            # Trigger validity analysis task
            analyze_validity_task.delay(challenge.id, request.user.id)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PatentAnalysisSummaryView(APIView):
    """
    Get summary statistics for patent analysis.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get patent analysis summary statistics",
        tags=["IP Litigation - Analytics"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: PatentAnalysisSummarySerializer}
    )
    def get(self, request):
        """Get patent analysis summary statistics"""
        start_time = time.time()
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)

        # Get summary statistics
        patents = PatentDocument.objects.filter(analysis_run=analysis_run, user=request.user)
        claims = PatentClaim.objects.filter(analysis_run=analysis_run, user=request.user)
        prior_art = PriorArtDocument.objects.filter(analysis_run=analysis_run, user=request.user)
        claim_charts = ClaimChart.objects.filter(analysis_run=analysis_run, user=request.user)
        infringement_analyses = InfringementAnalysis.objects.filter(analysis_run=analysis_run, user=request.user)
        validity_challenges = ValidityChallenge.objects.filter(analysis_run=analysis_run, user=request.user)

        # Patent office breakdown
        patent_office_breakdown = {}
        for office_choice in PatentDocument._meta.get_field('patent_office').choices:
            office_code, office_name = office_choice
            count = patents.filter(patent_office=office_code).count()
            if count > 0:
                patent_office_breakdown[office_name] = count

        # Litigation type breakdown
        litigation_type_breakdown = {
            analysis_run.get_litigation_type_display(): 1
        }

        # Technology area breakdown
        technology_area_breakdown = {}
        if analysis_run.technology_area:
            technology_area_breakdown[analysis_run.technology_area] = patents.count()

        summary_data = {
            'total_patents': patents.count(),
            'total_claims': claims.count(),
            'total_prior_art': prior_art.count(),
            'total_claim_charts': claim_charts.count(),
            'infringement_analyses': infringement_analyses.count(),
            'validity_challenges': validity_challenges.count(),
            'patent_office_breakdown': patent_office_breakdown,
            'litigation_type_breakdown': litigation_type_breakdown,
            'technology_area_breakdown': technology_area_breakdown
        }

        serializer = PatentAnalysisSummarySerializer(summary_data)
        response_data = {'summary': serializer.data, 'analysis_run_id': analysis_run_id, 'case_name': analysis_run.case_name}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="IP Litigation Patent Analysis",
                    service_id="ip-patent-analysis",
                    vertical="IP Litigation",
                    response_data=response_data,
                    user=request.user,
                    run=analysis_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="patent-analysis-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"case_name": analysis_run.case_name, "total_patents": summary_data['total_patents']}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate patent analysis report: {e}")

        return Response(response_data)


class ClaimChartSummaryView(APIView):
    """
    Get summary statistics for claim charts.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get claim chart summary statistics",
        tags=["IP Litigation - Analytics"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: ClaimChartSummarySerializer(many=True)}
    )
    def get(self, request):
        """Get claim chart summary statistics"""
        start_time = time.time()
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)

        # Get summary statistics grouped by chart type
        summary_data = []
        chart_types = ClaimChart.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        ).values_list('chart_type', flat=True).distinct()

        for chart_type in chart_types:
            charts = ClaimChart.objects.filter(
                analysis_run=analysis_run,
                user=request.user,
                chart_type=chart_type
            )

            summary_data.append({
                'chart_type': chart_type,
                'chart_type_display': dict(ClaimChart._meta.get_field('chart_type').choices)[chart_type],
                'total_count': charts.count(),
                'infringes_count': charts.filter(overall_conclusion='infringes').count(),
                'does_not_infringe_count': charts.filter(overall_conclusion='does_not_infringe').count(),
                'unclear_count': charts.filter(overall_conclusion='unclear').count(),
                'avg_confidence_score': charts.aggregate(Avg('confidence_score'))['confidence_score__avg'] or 0.0,
            })

        serializer = ClaimChartSummarySerializer(summary_data, many=True)
        response_data = {'summary': serializer.data, 'analysis_run_id': analysis_run_id, 'case_name': analysis_run.case_name}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="IP Litigation Claim Chart Summary",
                    service_id="ip-claim-chart",
                    vertical="IP Litigation",
                    response_data=response_data,
                    user=request.user,
                    run=analysis_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="claim-chart-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"case_name": analysis_run.case_name, "chart_types_count": len(summary_data)}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate claim chart report: {e}")

        return Response(response_data)


class InfringementSummaryView(APIView):
    """
    Get summary statistics for infringement analyses.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get infringement analysis summary statistics",
        tags=["IP Litigation - Analytics"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: InfringementSummarySerializer}
    )
    def get(self, request):
        """Get infringement analysis summary statistics"""
        start_time = time.time()
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)

        # Get infringement analysis summary
        analyses = InfringementAnalysis.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        )

        total_analyses = analyses.count()
        if total_analyses == 0:
            summary_data = {
                'total_analyses': 0,
                'infringement_found': 0,
                'no_infringement': 0,
                'mixed_results': 0,
                'inconclusive': 0,
                'literal_infringement_rate': 0.0,
                'doe_infringement_rate': 0.0,
                'high_confidence_analyses': 0
            }
        else:
            infringement_found = analyses.filter(infringement_conclusion='infringes').count()
            no_infringement = analyses.filter(infringement_conclusion='does_not_infringe').count()
            mixed_results = analyses.filter(infringement_conclusion='mixed').count()
            inconclusive = analyses.filter(infringement_conclusion='inconclusive').count()

            literal_infringement = analyses.filter(literal_infringement='yes').count()
            doe_infringement = analyses.filter(doctrine_of_equivalents='yes').count()
            high_confidence = analyses.filter(confidence_level='high').count()

            summary_data = {
                'total_analyses': total_analyses,
                'infringement_found': infringement_found,
                'no_infringement': no_infringement,
                'mixed_results': mixed_results,
                'inconclusive': inconclusive,
                'literal_infringement_rate': (literal_infringement / total_analyses) * 100,
                'doe_infringement_rate': (doe_infringement / total_analyses) * 100,
                'high_confidence_analyses': high_confidence
            }

        serializer = InfringementSummarySerializer(summary_data)
        response_data = {'summary': serializer.data, 'analysis_run_id': analysis_run_id, 'case_name': analysis_run.case_name}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="IP Litigation Infringement Analysis",
                    service_id="ip-infringement",
                    vertical="IP Litigation",
                    response_data=response_data,
                    user=request.user,
                    run=analysis_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="infringement-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"case_name": analysis_run.case_name, "total_analyses": total_analyses}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate infringement report: {e}")

        return Response(response_data)


class ValiditySummaryView(APIView):
    """
    Get summary statistics for validity challenges.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    @swagger_auto_schema(
        operation_description="Get validity challenge summary statistics",
        tags=["IP Litigation - Analytics"],
        manual_parameters=[
            openapi.Parameter('analysis_run_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('service_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('generate_report', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        ],
        responses={200: ValiditySummarySerializer}
    )
    def get(self, request):
        """Get validity challenge summary statistics"""
        start_time = time.time()
        analysis_run_id = request.query_params.get('analysis_run_id')
        if not analysis_run_id:
            return Response({"error": "analysis_run_id parameter is required"},
                          status=status.HTTP_400_BAD_REQUEST)

        # Ensure user owns the analysis run (lookup by UUID)
        analysis_run = get_object_or_404(PatentAnalysisRun, analysis_run_uuid=analysis_run_id, run__user=request.user)

        # Get validity challenge summary
        challenges = ValidityChallenge.objects.filter(
            analysis_run=analysis_run,
            user=request.user
        )

        total_challenges = challenges.count()
        strong_challenges = challenges.filter(challenge_strength='strong').count()
        moderate_challenges = challenges.filter(challenge_strength='moderate').count()
        weak_challenges = challenges.filter(challenge_strength='weak').count()

        avg_success_likelihood = challenges.aggregate(Avg('success_likelihood'))['success_likelihood__avg'] or 0.0

        # Count challenges by ground
        anticipation_challenges = 0
        obviousness_challenges = 0
        for challenge in challenges:
            if 'anticipation' in challenge.challenge_grounds:
                anticipation_challenges += 1
            if 'obviousness' in challenge.challenge_grounds:
                obviousness_challenges += 1

        # Most challenged patents
        most_challenged_patents = []
        patent_challenge_counts = {}
        for challenge in challenges:
            patent_num = challenge.target_patent.patent_number
            patent_challenge_counts[patent_num] = patent_challenge_counts.get(patent_num, 0) + 1

        # Sort by challenge count and take top 5
        sorted_patents = sorted(patent_challenge_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        most_challenged_patents = [{'patent_number': patent, 'challenge_count': count} for patent, count in sorted_patents]

        summary_data = {
            'total_challenges': total_challenges,
            'strong_challenges': strong_challenges,
            'moderate_challenges': moderate_challenges,
            'weak_challenges': weak_challenges,
            'avg_success_likelihood': avg_success_likelihood,
            'anticipation_challenges': anticipation_challenges,
            'obviousness_challenges': obviousness_challenges,
            'most_challenged_patents': most_challenged_patents
        }

        serializer = ValiditySummarySerializer(summary_data)
        response_data = {'summary': serializer.data, 'analysis_run_id': analysis_run_id, 'case_name': analysis_run.case_name}

        # Generate report if requested
        project_id = request.query_params.get('project_id')
        service_id = request.query_params.get('service_id')
        generate_report = request.query_params.get('generate_report', 'false').lower() == 'true'

        if generate_report and project_id and service_id:
            execution_time = time.time() - start_time
            try:
                report_info = generate_and_register_service_report(
                    service_name="IP Litigation Validity Summary",
                    service_id="ip-validity",
                    vertical="IP Litigation",
                    response_data=response_data,
                    user=request.user,
                    run=analysis_run.run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="validity-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"case_name": analysis_run.case_name, "total_challenges": total_challenges}
                )
                response_data['report_file'] = report_info
            except Exception as e:
                logger.warning(f"Failed to generate validity report: {e}")

        return Response(response_data)


class ServiceExecutionListCreateView(APIView):
    """
    API view for listing and creating service executions for IP Litigation.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        """List service executions with optional filtering."""
        try:
            # Get query parameters
            patent_analysis_run_id = request.query_params.get('patent_analysis_run_id')
            service_type = request.query_params.get('service_type')
            status = request.query_params.get('status')

            # Base queryset
            queryset = ServiceExecution.objects.filter(user=request.user)

            # Apply filters
            if patent_analysis_run_id:
                queryset = queryset.filter(patent_analysis_run_id=patent_analysis_run_id)
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
                    'patent_analysis_run_id': execution.patent_analysis_run.id if execution.patent_analysis_run else None,
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

            # Get patent analysis run if provided
            patent_analysis_run = None
            if 'patent_analysis_run_id' in data:
                try:
                    patent_analysis_run = PatentAnalysisRun.objects.get(
                        id=data['patent_analysis_run_id'],
                        user=request.user
                    )
                except PatentAnalysisRun.DoesNotExist:
                    return Response({
                        'success': False,
                        'error': 'Patent analysis run not found'
                    }, status=status.HTTP_404_NOT_FOUND)

            # Create service execution
            execution = ServiceExecution.objects.create(
                user=request.user,
                client=get_user_client(request.user),
                patent_analysis_run=patent_analysis_run,
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
    API view for listing and creating service outputs for IP Litigation.
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
