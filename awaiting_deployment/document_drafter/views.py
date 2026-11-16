# app_layout/views.py

from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import get_object_or_404
from django.db import transaction
from core.models import File

from rest_framework.decorators import action

from core.models import File, Run, Storage

from django.conf import settings
from django.utils.timezone import now

import hashlib


from celery.result import AsyncResult
from django_celery_results.models import TaskResult  # Optional for deeper results inspection

from .models import (
    CatalogEntry,
    ChatHistory,
    TaskStatus,
    ClientPersona,
    ProposalDraft,
    SuggestedSolution,
    SolutionChat,
    Notification,
    IncomingRFP
)
from .serializers import (
    FileSerializer,
    CatalogEntrySerializer,
    ChatHistorySerializer,
    TaskStatusSerializer,
    ClientPersonaSerializer,
    ProposalDraft,
    ProposalDraftSerializer,
    NotificationSerializer,
    IncomingRFPSerializer
)
from .utils import (
    build_client_persona_prompt,
    call_gpt4o,
    build_solution_chat_prompt,
    generate_proposal_docx_from_markdown
)
from .tasks import (
    process_document_task,
    generate_client_persona_task,
    generate_proposal_draft_task,
    generate_suggested_solutions_task,
    extract_rfp_sections_task,
    answer_rfp_chat_task,
    extract_rfp_metadata_task

)

from django.http import HttpResponse

# in app_layout/views.py
from oauth2_provider.models import Application
from oauth2_provider.generators import generate_client_secret
from django.contrib.auth import get_user_model
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import secrets
import string


from oauth2_provider.models import AccessToken
from django.utils import timezone


import os
import json

User = get_user_model()

SECRETS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs', '.secrets.json')


# ------------------------------------------
# Documents Already Uploaded - Link to Core
# ------------------------------------------
class UploadedDocumentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = FileSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        user = self.request.user
        return File.objects.filter(user=user).order_by('-created_at')

# ------------------------------------------
# Catalog Entries Linked to User Documents
# ------------------------------------------
class CatalogEntryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CatalogEntrySerializer

    def get_queryset(self):
        document_id = self.request.query_params.get('document_id')
        if document_id:
            return CatalogEntry.objects.filter(document_id=document_id)
        return CatalogEntry.objects.none()

# ------------------------------------------
# Chat History (per document)
# ------------------------------------------
class ChatHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ChatHistorySerializer

    def get_queryset(self):
        document_id = self.request.query_params.get('document_id')
        if document_id:
            return ChatHistory.objects.filter(document_id=document_id).order_by('-created_at')
        return ChatHistory.objects.none()

# ------------------------------------------
# Task Status (Catalog Extraction etc.)
# ------------------------------------------
class TaskStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        task_status = TaskStatus.objects.filter(document_id=document_id).order_by('-created_at').first()
        if task_status:
            serializer = TaskStatusSerializer(task_status)
            return Response(serializer.data)
        return Response({"status": "unknown"}, status=404)

# ------------------------------------------
# Ask Questions to Documents (Mini RAG Chat)
# ------------------------------------------
class AskQuestionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        document_id = request.data.get('document_id')
        question = request.data.get('question')

        if not document_id or not question:
            return Response({"error": "document_id and question are required"}, status=400)

        entries = CatalogEntry.objects.filter(document_id=document_id)
        if not entries.exists():
            return Response({"error": "No catalog entries found for document"}, status=404)

        context = "\n\n".join([
            f"Name: {e.name}\nDetails: {e.details}\nSolves: {e.solves}\nTarget Clients: {e.target_clients}" for e in entries
        ])
        prompt = f"Here is the context:\n{context}\n\nNow answer this question about the document:\n{question}"
        gpt_response = call_gpt4o(prompt)

        if isinstance(gpt_response, dict):
            answer = gpt_response.get("answer", "I'm sorry, I couldn’t find a proper answer.")
        else:
            answer = gpt_response

        ChatHistory.objects.create(document_id=document_id, user=request.user, question=question, answer=answer)
        return Response({"answer": answer})

# ------------------------------------------
# Generate Client Persona (Backgrounded)
# ------------------------------------------
class GenerateClientPersonaView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company_name = request.data.get('company_name')
        if not company_name:
            return Response({"error": "company_name is required"}, status=400)

        # Check if already exists (match inside standardized_persona)
        existing_persona = None
        for persona in ClientPersona.objects.all():
            standardized_data = persona.standardized_persona or {}
            if standardized_data.get("company_name", "").lower() == company_name.lower():
                existing_persona = persona
                break

        if existing_persona:
            serializer = ClientPersonaSerializer(existing_persona)
            return Response(serializer.data)

        # Otherwise start background task
        generate_client_persona_task.delay(company_name, request.user.id)
        return Response({"message": "Persona generation started. Please refresh after a while."}, status=202)

# ------------------------------------------
# List All Client Personas
# ------------------------------------------
class ClientPersonaListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        personas = ClientPersona.objects.filter(user=request.user).order_by('-created_at')
        serializer = ClientPersonaSerializer(personas, many=True)
        return Response(serializer.data)

# ------------------------------------------
# Auto-draft Proposal based on persona + extra info (Backgrounded)
# ------------------------------------------
class GenerateProposalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        persona_id = request.data.get('persona_id')
        additional_info = request.data.get('additional_info', '')
        focus_solution = request.data.get('focus_solution')  # Optional
        context_solutions = request.data.get('context_solutions')  # Optional

        if not persona_id:
            return Response({"error": "persona_id is required"}, status=400)

        # Make sure persona exists for this user
        persona = get_object_or_404(ClientPersona, id=persona_id, user=request.user)

        # Dispatch background task
        generate_proposal_draft_task.delay(
            client_persona_id=persona_id,
            additional_info=additional_info,
            focus_solution=focus_solution,
            context_solutions=context_solutions
        )

        return Response({"message": "Proposal generation started. Check back later."}, status=202)


# ------------------------------------------
# All Task Statuses for the User
# ------------------------------------------
class AllTaskStatusesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        statuses = TaskStatus.objects.filter(document__user=request.user).order_by('-created_at')
        serializer = TaskStatusSerializer(statuses, many=True)
        return Response(serializer.data)


# ------------------------------------------
# Proposal Drafts ViewSet (List generated proposals)
# ------------------------------------------
class ProposalDraftViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ProposalDraftSerializer

    def get_queryset(self):
        # Get the most recent proposal per persona (filtering out duplicates)
        return ProposalDraft.objects.filter(persona__user=self.request.user).order_by('persona', '-created_at').distinct('persona')



class MatchPersonaToCatalogView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        persona_id = request.data.get('persona_id')
        if not persona_id:
            return Response({"error": "persona_id is required"}, status=400)

        persona = get_object_or_404(ClientPersona, id=persona_id, user=request.user)

        # Launch matching task
        from app_layout.tasks import match_persona_to_catalog_task
        match_persona_to_catalog_task.delay(persona_id)

        return Response({"message": "Matching started. Please refresh after a while."}, status=202)


class SuggestedSolutionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        persona_id = request.query_params.get('persona_id')
        if not persona_id:
            return Response({"error": "persona_id is required"}, status=400)

        persona = get_object_or_404(ClientPersona, id=persona_id, user=request.user)
        solutions = persona.suggested_solutions.all().order_by('-created_at')

        from app_layout.serializers import SuggestedSolutionSerializer
        serializer = SuggestedSolutionSerializer(solutions, many=True)
        return Response(serializer.data)

class GenerateSuggestedSolutionsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        persona_id = request.data.get('persona_id')
        if not persona_id:
            return Response({"error": "persona_id is required"}, status=400)

        persona = get_object_or_404(ClientPersona, id=persona_id, user=request.user)

        generate_suggested_solutions_task.delay(persona.id)

        return Response({"message": "Suggested solutions generation started. Please refresh later."}, status=202)
    


def save_secret(email, client_id, client_secret):
    if not os.path.exists(os.path.dirname(SECRETS_FILE)):
        os.makedirs(os.path.dirname(SECRETS_FILE))

    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE, 'r') as f:
            data = json.load(f)
    else:
        data = {}

    data[email] = {
        "client_id": client_id,
        "client_secret": client_secret
    }

    with open(SECRETS_FILE, 'w') as f:
        json.dump(data, f, indent=4)


def load_secret(email):
    if not os.path.exists(SECRETS_FILE):
        return None

    with open(SECRETS_FILE, 'r') as f:
        data = json.load(f)

    return data.get(email)


class SignupView(APIView):
    permission_classes = []
    authentication_classes = []

    def get(self, request):
        return Response({"message": "Please use POST to sign up."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def post(self, request, *args, **kwargs):
        email = request.data.get('email')
        password = request.data.get('password')

        if not email.endswith('@morae.com'):
            return Response({"error": "Only @morae.com emails are allowed."}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=email).exists():
            return Response({"error": "User already exists."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.create_user(email=email, password=password)

        # Generate raw client secret
        raw_client_secret = generate_client_secret()

        application = Application.objects.create(
            user=user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_PASSWORD,
            name=f"API Access for {user.email}",
            client_secret=raw_client_secret,  # Django hashes internally
        )

        # Save raw credentials to secrets file
        save_secret(user.email, application.client_id, raw_client_secret)

        return Response({
            "email": user.email,
            "client_id": application.client_id,
            "client_secret": raw_client_secret,
        }, status=status.HTTP_201_CREATED)


class FetchCredentialsView(APIView):
    permission_classes = []
    authentication_classes = []

    def get(self, request):
        return Response({"message": "Please use POST to fetch credentials."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def post(self, request, *args, **kwargs):
        email = request.data.get('email')

        creds = load_secret(email)
        if not creds:
            return Response({"error": "Credentials not found. Please signup again."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "email": email,
            "client_id": creds['client_id'],
            "client_secret": creds['client_secret']
        }, status=status.HTTP_200_OK)
        
        
class AskSolutionQuestionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        solution_id = request.data.get('solution_id')
        question = request.data.get('question')
        context = request.data.get('context', '')  # 🆕 allow optional context

        if not solution_id or not question:
            return Response({"error": "solution_id and question are required"}, status=400)

        solution = get_object_or_404(SuggestedSolution, id=solution_id, persona__user=request.user)

        # Combine context + question in prompt
        prompt = build_solution_chat_prompt(solution, question, context=context)  # 🆕 pass context
        answer = call_gpt4o(prompt)

        if isinstance(answer, dict):
            answer = answer.get("answer", "Sorry, couldn't generate an answer.")

        # Save the interaction
        SolutionChat.objects.create(solution=solution, user=request.user, question=question, answer=answer)

        return Response({"answer": answer})




class DownloadProposalDocxView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, proposal_id):
        proposal = get_object_or_404(ProposalDraft, id=proposal_id, persona__user=request.user)

        markdown = f"""
## Proposal for {proposal.persona.standardized_persona.get("company_name", "N/A")}

**Notes:** {proposal.additional_notes or 'N/A'}

---

{proposal.content}
        """

        buffer = generate_proposal_docx_from_markdown(
            proposal.persona.standardized_persona.get("company_name", "N/A"),
            proposal.additional_notes or "No additional notes provided.",
            markdown
        )

        response = HttpResponse(
            buffer.read(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        response["Content-Disposition"] = f"attachment; filename=proposal_{proposal.id}.docx"
        return response


class NotificationViewSet(viewsets.ModelViewSet):
    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    # ------- READ / UNREAD -------
    @action(detail=True, methods=['post'])
    def mark_as_read(self, request, pk=None):
        notification = self.get_object()
        notification.read = True
        notification.save()
        return Response({'status': 'marked as read'})

    @action(detail=True, methods=['post'])
    def mark_as_unread(self, request, pk=None):
        notification = self.get_object()
        notification.read = False
        notification.save()
        return Response({'status': 'marked as unread'})

    @action(detail=False, methods=['post'])
    def mark_all_as_read(self, request):
        Notification.objects.filter(user=request.user, read=False).update(read=True)
        return Response({'status': 'all marked as read'})

    @action(detail=False, methods=['post'])
    def mark_all_as_unread(self, request):
        Notification.objects.filter(user=request.user, read=True).update(read=False)
        return Response({'status': 'all marked as unread'})

    # ------- ARCHIVED / UNARCHIVED -------
    @action(detail=True, methods=['post'])
    def mark_as_archived(self, request, pk=None):
        notification = self.get_object()
        notification.archived = True
        notification.save()
        return Response({'status': 'archived'})

    @action(detail=True, methods=['post'])
    def mark_as_unarchived(self, request, pk=None):
        notification = self.get_object()
        notification.archived = False
        notification.save()
        return Response({'status': 'unarchived'})

    @action(detail=False, methods=['post'])
    def mark_all_as_archived(self, request):
        Notification.objects.filter(user=request.user).update(archived=True)
        return Response({'status': 'all archived'})

    @action(detail=False, methods=['post'])
    def mark_all_as_unarchived(self, request):
        Notification.objects.filter(user=request.user).update(archived=False)
        return Response({'status': 'all unarchived'})

    # ------- IMPORTANT / NOT IMPORTANT -------
    @action(detail=True, methods=['post'])
    def mark_as_important(self, request, pk=None):
        notification = self.get_object()
        notification.important = True
        notification.save()
        return Response({'status': 'marked as important'})

    @action(detail=True, methods=['post'])
    def mark_as_not_important(self, request, pk=None):
        notification = self.get_object()
        notification.important = False
        notification.save()
        return Response({'status': 'marked as not important'})

    @action(detail=False, methods=['post'])
    def mark_all_as_important(self, request):
        Notification.objects.filter(user=request.user).update(important=True)
        return Response({'status': 'all marked as important'})

    @action(detail=False, methods=['post'])
    def mark_all_as_not_important(self, request):
        Notification.objects.filter(user=request.user).update(important=False)
        return Response({'status': 'all marked as not important'})

    # ------- DELETED / NOT DELETED -------
    @action(detail=True, methods=['post'])
    def mark_as_deleted(self, request, pk=None):
        notification = self.get_object()
        notification.deleted = True
        notification.save()
        return Response({'status': 'marked as deleted'})

    @action(detail=True, methods=['post'])
    def mark_as_not_deleted(self, request, pk=None):
        notification = self.get_object()
        notification.deleted = False
        notification.save()
        return Response({'status': 'marked as not deleted'})

    @action(detail=False, methods=['post'])
    def mark_all_as_deleted(self, request):
        Notification.objects.filter(user=request.user).update(deleted=True)
        return Response({'status': 'all marked as deleted'})

    @action(detail=False, methods=['post'])
    def mark_all_as_not_deleted(self, request):
        Notification.objects.filter(user=request.user).update(deleted=False)
        return Response({'status': 'all marked as not deleted'})


class UploadIncomingRFPView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({"error": "No file provided."}, status=400)

        user = request.user
        timestamp = now().strftime("%Y%m%d%H%M%S")

        # Compute MD5 hash
        md5 = hashlib.md5()
        for chunk in uploaded_file.chunks():
            md5.update(chunk)
        file_hash = md5.hexdigest()

        # Prevent duplicate uploads
        if File.objects.filter(md5_hash=file_hash, user=user).exists():
            return Response({"error": "This document already exists."}, status=409)

        # Reset file pointer before writing to disk
        uploaded_file.seek(0)

        # Construct and create upload path
        relative_upload_path = os.path.join("uploads", "rfps", f"{timestamp}_{uploaded_file.name}")
        full_upload_path = os.path.join(settings.MEDIA_ROOT, relative_upload_path)
        os.makedirs(os.path.dirname(full_upload_path), exist_ok=True)

        with open(full_upload_path, "wb+") as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)

        # Create run + storage
        run = Run.objects.create(user=user)
        storage = Storage.objects.create(user=user, run=run, upload_storage_location=full_upload_path)

        # Register file in DB
        file_obj = File.objects.create(
            user=user,
            filename=uploaded_file.name,
            file_size=uploaded_file.size,
            file_type=uploaded_file.content_type,
            filepath=relative_upload_path,  # Relative to MEDIA_ROOT
            md5_hash=file_hash,
            run=run,
            topic="rfp",
            project_id="rfp-ingest",
            service_id="rfp-parser",
            storage=storage
        )

        # Create Incoming RFP record
        rfp = IncomingRFP.objects.create(
            file=file_obj,
            user=user,
            status="processing",
            client_name="",  # Filled by GPT later
            due_date=None
        )

        # Trigger Celery background jobs
        extract_rfp_metadata_task.delay(rfp.id)
        extract_rfp_sections_task.delay(rfp.id)

        return Response({
            "message": "RFP uploaded successfully. GPT is extracting metadata and sections.",
            "rfp_id": rfp.id,
            "run_id": str(run.run_id)
        }, status=201)
 
    
class IncomingRFPListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rfps = IncomingRFP.objects.filter(file__user=request.user).order_by('-created_at')
        serializer = IncomingRFPSerializer(rfps, many=True)
        return Response(serializer.data)


class AskRFPQuestionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        rfp_id = request.data.get('rfp_id')
        question = request.data.get('question')
        history = request.data.get('history', '')
        sync = request.data.get('sync', True)

        if not rfp_id or not question:
            return Response({"error": "rfp_id and question are required"}, status=400)

        if sync:
            # inline response (draft or chat)
            response = answer_rfp_chat_task(rfp_id, question, history)
            return Response({"answer": response})
        
        # fallback async background
        task = answer_rfp_chat_task.delay(rfp_id, question, history)
        return Response({
            "message": "GPT response is being generated.",
            "task_id": task.id
        }, status=202)


'''
class AskRFPQuestionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        rfp_id = request.data.get('rfp_id')
        question = request.data.get('question')
        history = request.data.get('history', '')

        if not rfp_id or not question:
            return Response({"error": "rfp_id and question are required"}, status=400)

        # Trigger background chat generation
        task = answer_rfp_chat_task.delay(rfp_id, question, history)

        return Response({
            "message": "GPT response is being generated.",
            "task_id": task.id
        }, status=202)
'''


class GetRFPAnswerStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, task_id):
        result = AsyncResult(task_id)
        if result.ready():
            return Response({
                "status": result.status,
                "result": result.result
            })
        else:
            return Response({"status": result.status}, status=202)


class IncomingRFPDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        rfp = get_object_or_404(IncomingRFP, id=pk, user=request.user)
        serializer = IncomingRFPSerializer(rfp)
        return Response(serializer.data)

    def patch(self, request, pk):
        rfp = get_object_or_404(IncomingRFP, id=pk, user=request.user)
        serializer = IncomingRFPSerializer(rfp, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=400)


class RFPChatHistoryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, rfp_id):
        chats = RFPChatHistory.objects.filter(rfp__id=rfp_id, user=request.user).order_by('-created_at')
        serializer = RFPChatHistorySerializer(chats, many=True)
        return Response(serializer.data)


# ------------------------------------------
# Check RFP Upload + Parsing Status
# ------------------------------------------
class RFPProcessingStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        run_id = request.query_params.get('run_id')
        if not run_id:
            return Response({"error": "Missing run_id"}, status=400)

        # CORRECTED query
        rfp = IncomingRFP.objects.filter(file__run__run_id=run_id, file__user=request.user).first()
        if not rfp:
            return Response({"error": "RFP not found"}, status=404)

        return Response({
            "status": rfp.status,
            "summary": rfp.summary,
            "parsed_sections": rfp.parsed_sections,
            "rfp_id": rfp.id,
            "client_name": rfp.client_name,
            "due_date": rfp.due_date
        })


class GenerateRFPDocxView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company_name = request.data.get("company_name", "Client")
        notes = request.data.get("notes", "")
        markdown = request.data.get("markdown_content", "")

        if not markdown:
            return Response({"error": "Missing markdown_content"}, status=400)

        buffer = generate_proposal_docx_from_markdown(company_name, notes, markdown)

        response = HttpResponse(
            buffer.read(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        response["Content-Disposition"] = f"attachment; filename=rfp-response.docx"
        return response

