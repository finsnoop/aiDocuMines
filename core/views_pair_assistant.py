from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import ai_harness as harness
from core.pair_assistant import CATALOGUE, PairProviderError, analyse
from core.views_assistant import AssistantThrottle, _assistant_allowed


class PairPresetsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _assistant_allowed(request.user):
            return Response({'error':'The assistant is not available for your role or organisation.'},status=403)
        return Response(CATALOGUE)


class PairAssistantView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AssistantThrottle]

    def post(self, request):
        if not _assistant_allowed(request.user):
            return Response({'error':'The assistant is not available for your role or organisation.'},status=403)
        if not harness.surface_allowed(request.user, 'drafting'):
            return Response({'error':'Your administrator has turned off Ask Assistant on this page.'},status=403)
        question = str(request.data.get('question') or '') if isinstance(request.data, dict) else ''
        settings = harness.settings_for(request.user, harness.PROFILES['drafting.pair'])
        if harness.screen_question(question) and settings['guardrails']['prompt_injection'] == 'block':
            return Response({'error':"That question asks the Assistant to set aside its rules, so it won't be answered. "
                                     'Ask about the two documents instead.'},status=400)
        try:
            result = analyse(request.user, request.data)
        except PairProviderError:
            return Response({'error':'The configured AI model could not complete a grounded document-pair analysis. Please try again.'},status=502)
        from custom_authentication.models import UserActivityLog
        UserActivityLog.objects.create(user=request.user,event='CHAT_QUERY',metadata={
            'context_type':'document_pair','original_file_id':request.data.get('original_file_id'),
            'processed_file_id':request.data.get('processed_file_id'),'preset_id':request.data.get('preset_id'),
            'prompt_version':result['prompt_version'],'pair_fingerprint':result['document_fingerprint'],
            'relationship':result['relationship'],'cache_hit':result['cache_hit'],
            'sources':len(result['sources']),'web':'off','surface':'drafting',
            'flags':['injection'] if harness.screen_question(question) else []})
        return Response(result)
