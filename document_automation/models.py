from django.db import models
from core.models import File
from django.conf import settings
import uuid


class ClauseCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, db_index=True)
    description = models.TextField(blank=True, null=True)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    project_id = models.CharField(max_length=255, db_index=True)
    client_name = models.CharField(max_length=255, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Clause categories"
        ordering = ['name']

    def __str__(self):
        return self.name


class Clause(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(ClauseCategory, on_delete=models.CASCADE, related_name='clauses', null=True, blank=True)
    title = models.CharField(max_length=255, db_index=True)
    content = models.TextField()
    version = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    client_name = models.CharField(max_length=255, db_index=True)
    variables = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title', '-version']

    def __str__(self):
        return f"{self.title} (v{self.version})"


class AutomationTemplate(models.Model):
    TEMPLATE_TYPE_CHOICES = [
        ("DOCX", "DOCX"),
        ("TXT", "TXT"),
        ("HTML", "HTML"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, db_index=True)
    description = models.TextField(blank=True, null=True)
    file = models.ForeignKey(File, on_delete=models.SET_NULL, null=True, blank=True, related_name="automation_templates")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="automation_template_definitions")
    template_type = models.CharField(max_length=10, choices=TEMPLATE_TYPE_CHOICES, default="TXT", db_index=True)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    client_name = models.CharField(max_length=255, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AutomationTemplate {self.name} ({self.template_type})"


class TemplateField(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(AutomationTemplate, on_delete=models.CASCADE, related_name='fields')
    name = models.CharField(max_length=255)
    field_type = models.CharField(max_length=50, default='text')
    required = models.BooleanField(default=False)
    default_value = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    options = models.JSONField(blank=True, null=True)
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order']
        unique_together = ('template', 'name')

    def __str__(self):
        return f"{self.template.name} - {self.name}"


class AutomationRun(models.Model):
    STATUS_CHOICES = [
        ("Pending", "Pending"),
        ("Processing", "Processing"),
        ("Completed", "Completed"),
        ("Failed", "Failed"),
    ]

    OUTPUT_FORMAT_CHOICES = [
        ("DOCX", "DOCX"),
        ("PDF", "PDF"),
        ("BOTH", "BOTH"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    client_name = models.CharField(max_length=255, db_index=True)
    template = models.ForeignKey(AutomationTemplate, on_delete=models.CASCADE, related_name="runs")
    input_data = models.JSONField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Pending", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    error_message = models.TextField(blank=True, null=True)
    bulk_count = models.IntegerField(default=1)
    bulk_data = models.JSONField(blank=True, null=True)
    clause_ids = models.JSONField(blank=True, null=True)
    output_format = models.CharField(max_length=10, choices=OUTPUT_FORMAT_CHOICES, default="DOCX")

    def __str__(self):
        return f"AutomationRun {self.id} - {self.status}"


class AutomationResult(models.Model):
    STATUS_CHOICES = [
        ("Processing", "Processing"),
        ("Completed", "Completed"),
        ("Failed", "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AutomationRun, on_delete=models.CASCADE, related_name="results")
    output_filepath = models.CharField(max_length=1024, blank=True, null=True)
    pdf_output_path = models.CharField(max_length=1024, blank=True, null=True)
    output_filename = models.CharField(max_length=255, blank=True, null=True)
    variables_used = models.JSONField(blank=True, null=True)
    generation_index = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Processing", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AutomationResult {self.id} - {self.status}"
