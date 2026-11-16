from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from core.models import File  # We use File model for uploaded documents
from django.contrib.postgres.fields import JSONField

# ------------------------------------------
# Client (Tenant) Model
# ------------------------------------------
class Client(models.Model):
    name = models.CharField(max_length=255, unique=True)
    address = models.TextField(blank=True, null=True)
    industry = models.CharField(max_length=255, blank=True)
    use_case = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

# ------------------------------------------
# Custom User Manager
# ------------------------------------------
class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)

# ------------------------------------------
# Custom User Model
# ------------------------------------------
class User(AbstractBaseUser, PermissionsMixin):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, null=True, blank=True, related_name="users")
    email = models.EmailField(unique=True)
    contact_name = models.CharField(max_length=255, blank=True, null=True)
    contact_phone = models.CharField(max_length=50, blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    industry = models.CharField(max_length=255, blank=True, null=True)
    use_case = models.CharField(max_length=255, blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    class Meta:
        app_label = 'app_layout'

    def __str__(self):
        return self.email

# ------------------------------------------
# Catalog Entry Model (Products/Services)
# ------------------------------------------
class CatalogEntry(models.Model):
    document = models.ForeignKey(File, on_delete=models.CASCADE, related_name='catalog_entries')
    name = models.CharField(max_length=500)
    details = models.TextField()
    solves = models.TextField()
    target_clients = models.JSONField()
    page_hash = models.CharField(max_length=64, unique=True)

    def __str__(self):
        return self.name

# ------------------------------------------
# Client Persona Model
# ------------------------------------------
class ClientPersona(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='client_personas', null=True, blank=True)
    search_query = models.CharField(max_length=500)  # Example: "Alexander Forbes"
    standardized_persona = models.JSONField()  # The structured persona info
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.search_query

# ------------------------------------------
# Suggested Solutions Model
# ------------------------------------------
class SuggestedSolution(models.Model):
    persona = models.ForeignKey(ClientPersona, on_delete=models.CASCADE, related_name='suggested_solutions')
    product_or_service = models.CharField(max_length=500)
    reason_for_relevance = models.TextField()
    catalog_entry = models.ForeignKey(CatalogEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="suggested_solutions")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.product_or_service} for {self.persona.search_query}"


# ------------------------------------------
# Auto-Drafted Proposal Model
# ------------------------------------------
class ProposalDraft(models.Model):
    persona = models.ForeignKey(ClientPersona, on_delete=models.CASCADE, related_name='proposals')
    content = models.TextField()
    docx_file = models.FileField(upload_to="proposal_docs/", null=True, blank=True)  # 🆕 Save proposal nicely
    additional_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Proposal for {self.persona.search_query}"

# ------------------------------------------
# Chat History Model (Document Q&A)
# ------------------------------------------
class ChatHistory(models.Model):
    document = models.ForeignKey(File, on_delete=models.CASCADE, related_name='chat_history')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_questions', null=True, blank=True)
    question = models.TextField()
    answer = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

# ------------------------------------------
# Task Status Model (Background tasks tracking)
# ------------------------------------------
class TaskStatus(models.Model):
    TASK_CHOICES = [
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed')
    ]
    document = models.ForeignKey(File, on_delete=models.CASCADE, related_name='tasks')
    task_type = models.CharField(max_length=255)
    status = models.CharField(max_length=50, choices=TASK_CHOICES, default='processing')
    message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.task_type} for {self.document.filename} [{self.status}]"

# ------------------------------------------
# Proposal Suggestion Model (OLD)
# ------------------------------------------
class ProposalSuggestion(models.Model):
    persona = models.ForeignKey(ClientPersona, on_delete=models.CASCADE, related_name='proposal_suggestions')
    additional_info = models.TextField(blank=True, null=True)
    proposal_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Proposal Suggestion for {self.persona.search_query}"


# ------------------------------------------
# Chat History for Suggested Solutions
# ------------------------------------------
class SolutionChat(models.Model):
    solution = models.ForeignKey(SuggestedSolution, on_delete=models.CASCADE, related_name='chats')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    question = models.TextField()
    answer = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Chat on {self.solution.product_or_service} by {self.user.email}"


# ------------------------------------------
# Notification Model
# ------------------------------------------
class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    
    title = models.CharField(max_length=255)
    message = models.TextField()

    # Status flags
    read = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    important = models.BooleanField(default=False)
    deleted = models.BooleanField(default=False)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # Optional but helpful

    def __str__(self):
        return f"{self.title} for {self.user.email}"

    class Meta:
        ordering = ['-created_at']


# ------------------------------------------
# Incoming RFP Model
# ------------------------------------------
class IncomingRFP(models.Model):
    STATUS_CHOICES = [
        ('uploaded', 'Uploaded'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('responded', 'Responded'),
        ('archived', 'Archived'),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='incoming_rfps')
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name='rfps')
    client_name = models.CharField(max_length=255)
    due_date = models.DateField(null=True, blank=True)

    summary = models.TextField(blank=True, null=True)
    parsed_sections = models.JSONField(blank=True, null=True)

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='uploaded')
    
    draft = models.TextField(blank=True, null=True)  # Add this field

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"RFP from {self.client_name} ({self.file.filename})"


# ------------------------------------------
# Chat History for Incoming RFPs
# ------------------------------------------
class RFPChatHistory(models.Model):
    rfp = models.ForeignKey('IncomingRFP', on_delete=models.CASCADE, related_name='chats')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    question = models.TextField()
    answer = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Chat on RFP #{self.rfp.id} by {self.user.email}"

