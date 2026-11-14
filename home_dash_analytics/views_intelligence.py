from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Sum
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from core.access import accessible_files, get_files_for_user, get_file_for_user, tenant_id, is_tenant_admin
from core.models import File
from .views import OAuth2SecuredAPIView
from .models import DocumentIntelligence, IntelligenceRun, IntelligenceRule
from .intelligence import business_overview, build_intelligence, source_hash
from datetime import timedelta


class BusinessDashboardView(OAuth2SecuredAPIView):
    def get(self, request):
        if not tenant_id(request.user):
            raise PermissionDenied('An organisation is required.')
        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (ValueError, TypeError):
            raise ValidationError('Page must be an integer.')
        return Response(business_overview(request.user, request.query_params.get('project_id'),
                                          request.query_params.get('service_id'), page=page,
                                          bucket=request.query_params.get('bucket', ''),
                                          field=request.query_params.get('field', ''),
                                          currency=request.query_params.get('currency', '')))


class IntelligenceRunsView(OAuth2SecuredAPIView):
    def get(self, request):
        rows = IntelligenceRun.objects.filter(user=request.user, tenant_id=tenant_id(request.user))[:20]
        return Response([{'id': r.pk, 'status': r.status, 'completed': r.completed,
                          'total': len(r.file_ids), 'errors': r.errors} for r in rows])

    def post(self, request):
        if not tenant_id(request.user):
            raise PermissionDenied('An organisation is required.')
        if IntelligenceRun.objects.filter(user=request.user, status__in=['queued', 'running'],
                                         updated_at__gte=timezone.now() - timedelta(minutes=35)).exists():
            raise ValidationError('An analysis is already queued or running.')
        ids = request.data.get('file_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20:
            raise ValidationError('Select between 1 and 20 documents per analysis.')
        files = get_files_for_user(request.user, ids, 'process')
        if any(file.user.client_id != tenant_id(request.user) for file in files):
            raise PermissionDenied('All sources must belong to your organisation.')
        run = IntelligenceRun.objects.create(user=request.user, tenant_id=tenant_id(request.user),
                                              file_ids=[file.pk for file in files])
        try:
            build_intelligence.delay(str(run.pk))
        except Exception:
            run.status = 'failed'
            run.errors = [{'error': 'Analysis queue is unavailable; please retry.'}]
            run.save()
            return Response({'error': run.errors[0]['error']}, status=503)
        return Response({'id': run.pk, 'status': run.status}, status=202)


class IntelligenceSourcesView(OAuth2SecuredAPIView):
    def get(self, request):
        files = accessible_files(request.user, 'process', File.objects.filter(user__client_id=tenant_id(request.user)))
        if request.query_params.get('project_id'):
            files = files.filter(project_id=request.query_params['project_id'])
        # Connectors publish documents through the same core.File ACL boundary.
        return Response({'results': list(files.order_by('-created_at').values('id', 'filename', 'document_type',
                        'project_id', 'status')[:500]), 'total': files.count(),
                         'source_policy': 'Uploaded and connector-imported documents with document access grants. '
                         'Unconnected Microsoft services are not searched.'})


class IntelligenceReviewView(OAuth2SecuredAPIView):
    def post(self, request, file_id):
        file = get_file_for_user(request.user, file_id, 'rename')
        record = get_object_or_404(DocumentIntelligence, file=file, tenant_id=tenant_id(request.user))
        if source_hash(file.content or '') != record.source_hash:
            raise ValidationError('Source changed; rerun analysis before review.')
        record.reviewed_by = request.user
        record.reviewed_at = timezone.now()
        record.save(update_fields=['reviewed_by', 'reviewed_at'])
        return Response({'reviewed': True})


class RuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntelligenceRule
        fields = ['id', 'name', 'instruction', 'document_category', 'enabled']


class IntelligenceRulesView(OAuth2SecuredAPIView):
    def get(self, request):
        return Response(RuleSerializer(IntelligenceRule.objects.filter(tenant_id=tenant_id(request.user)), many=True).data)

    def post(self, request):
        if not is_tenant_admin(request.user) or not tenant_id(request.user):
            raise PermissionDenied('Only organisation administrators can configure compliance rules.')
        serializer = RuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(tenant_id=tenant_id(request.user))
        return Response(serializer.data, status=201)


class IntelligenceTokenUsageView(OAuth2SecuredAPIView):
    def get(self, request):
        from cost_centre.models import TokenUsage
        rows = TokenUsage.objects.filter(tenant_id=tenant_id(request.user))
        if not is_tenant_admin(request.user):
            rows = rows.filter(user=request.user)
        breakdown = list(rows.values('event_log__metadata__provider', 'event_log__metadata__model',
                                     'event_log__metadata__task').annotate(tokens=Sum('tokens_used')).order_by('-tokens'))
        return Response({'total': rows.aggregate(total=Sum('tokens_used'))['total'] or 0,
                         'breakdown': breakdown,
                         'coverage': 'Recorded AI calls. Uninstrumented integrations and calls without provider usage '
                                     'are not assumed to have used zero tokens.'})


class IntelligenceCompareView(OAuth2SecuredAPIView):
    def post(self, request):
        from difflib import SequenceMatcher
        from document_search.utils import embed_texts
        import numpy as np
        ids = request.data.get('file_ids')
        if not isinstance(ids, list) or len(ids) != 2 or ids[0] == ids[1]:
            raise ValidationError('Select two different documents.')
        files = get_files_for_user(request.user, ids, 'process')
        if any(f.user.client_id != tenant_id(request.user) for f in files):
            raise PermissionDenied('Documents must belong to your organisation.')
        texts = [f.content or '' for f in files]
        if any(not t.strip() or len(t) > 100000 for t in texts):
            raise ValidationError('Both sources need extracted text of at most 100,000 characters.')
        try:
            lexical_threshold = float(request.data.get('lexical_threshold', .8))
            semantic_threshold = float(request.data.get('semantic_threshold', .85))
            if not 0 <= lexical_threshold <= 1 or not 0 <= semantic_threshold <= 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError('Similarity thresholds must be between 0 and 1.')
        lexical = SequenceMatcher(None, texts[0].split(), texts[1].split(), autojunk=False).ratio()
        # Encode every chunk, rather than silently truncating a full contract at model context length.
        vectors = []
        for text in texts:
            chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
            vector = np.mean(embed_texts(chunks), axis=0)
            vectors.append(vector / max(float(np.linalg.norm(vector)), 1e-12))
        semantic = max(0.0, min(1.0, float(np.dot(*vectors))))
        return Response({'file_ids': ids, 'lexical_similarity': lexical, 'semantic_similarity': semantic,
                         'lexical_threshold': lexical_threshold, 'semantic_threshold': semantic_threshold,
                         'deviation': lexical < lexical_threshold or semantic < semantic_threshold,
                         'method': 'Token sequence similarity and mean chunk embedding cosine similarity; '
                                   'review aid, not a legal equivalence determination.'})
