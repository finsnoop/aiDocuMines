# app_layout/serializers.py

from rest_framework import serializers
from core.models import File
from .models import (
    CatalogEntry,
    ClientPersona,
    ProposalSuggestion,
    ChatHistory,
    TaskStatus,
    ProposalDraft,  # Correct model imported
    SuggestedSolution,
    SolutionChat,
    Notification,
    IncomingRFP,
    RFPChatHistory
)

# ------------------------------------------
# Serializer for Core Uploaded Files
# ------------------------------------------
class FileSerializer(serializers.ModelSerializer):
    class Meta:
        model = File
        fields = ['id', 'filename', 'filepath', 'file_size', 'file_type', 'created_at', 'updated_at']

# ------------------------------------------
# Serializer for Catalog Entries
# ------------------------------------------
class CatalogEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = CatalogEntry
        fields = [
            'id', 'document', 'name', 'details', 'solves', 'target_clients', 'page_hash'
        ]

# ------------------------------------------
# Serializer for Client Personas
# ------------------------------------------
class ClientPersonaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientPersona
        fields = [
            'id', 'search_query', 'standardized_persona', 'created_at'
        ]

# ------------------------------------------
# Serializer for Proposal Suggestions (Auto-drafts)
# ------------------------------------------
class ProposalSuggestionSerializer(serializers.ModelSerializer):
    persona_search_query = serializers.CharField(source="persona.search_query", read_only=True)

    class Meta:
        model = ProposalSuggestion
        fields = [
            'id', 'persona', 'persona_search_query', 'proposal_text', 'additional_info', 'created_at'
        ]

# ------------------------------------------
# Serializer for Proposal Drafts (the final Proposal DOCX generator)
# ------------------------------------------
class ProposalDraftSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProposalDraft
        fields = [
            'id', 'persona', 'content', 'docx_file', 'additional_notes', 'created_at'
        ]

# ------------------------------------------
# Serializer for Chat History
# ------------------------------------------
class ChatHistorySerializer(serializers.ModelSerializer):
    document_name = serializers.CharField(source="document.filename", read_only=True)

    class Meta:
        model = ChatHistory
        fields = [
            'id', 'document', 'document_name', 'user', 'question', 'answer', 'created_at'
        ]

# ------------------------------------------
# Serializer for Task Status (Background tasks tracking)
# ------------------------------------------
class TaskStatusSerializer(serializers.ModelSerializer):
    document_name = serializers.CharField(source="document.filename", read_only=True)

    class Meta:
        model = TaskStatus
        fields = [
            'id', 'document', 'document_name', 'task_type', 'status', 'message', 'created_at', 'updated_at'
        ]

# ------------------------------------------
# Serializer for Suggested Solutions
# ------------------------------------------     
class SuggestedSolutionSerializer(serializers.ModelSerializer):
    catalog_entry_name = serializers.CharField(source='catalog_entry.name', read_only=True)
    document_id = serializers.IntegerField(source='catalog_entry.document.id', read_only=True)

    class Meta:
        model = SuggestedSolution
        fields = ['id', 'product_or_service', 'reason_for_relevance', 'created_at', 'catalog_entry_name', 'document_id']

# ------------------------------------------
# Serializer for Solution Chat
# ------------------------------------------
class SolutionChatSerializer(serializers.ModelSerializer):
    class Meta:
        model = SolutionChat
        fields = '__all__'


# ------------------------------------------
# Serializer for Notifications
# ------------------------------------------
class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            'id',
            'title',
            'message',
            'read',
            'archived',
            'important',
            'deleted',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# ------------------------------------------
# Serializer for Incoming RFPs
# ------------------------------------------
class IncomingRFPSerializer(serializers.ModelSerializer):
    filename = serializers.CharField(source='file.filename', read_only=True)
    filepath = serializers.CharField(source='file.filepath', read_only=True)
    file_size = serializers.IntegerField(source='file.file_size', read_only=True)
    file_type = serializers.CharField(source='file.file_type', read_only=True)

    class Meta:
        model = IncomingRFP
        fields = [
            'id',
            'user',
            'file',
            'filename',
            'filepath',
            'file_size',
            'file_type',
            'client_name',
            'due_date',
            'summary',
            'parsed_sections',
            'status',
            'draft',  # add this line
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'file', 'filename', 'filepath', 'file_size', 'file_type']
        
        

# ------------------------------------------
# Serializer for RFP Chat History
# ------------------------------------------
class RFPChatHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = RFPChatHistory
        fields = ['id', 'rfp', 'user', 'question', 'answer', 'created_at']

