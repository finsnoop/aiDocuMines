from django.contrib import admin

from .models import Enquiry


@admin.register(Enquiry)
class EnquiryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "name", "email", "company", "topic", "practice_area", "handled")
    list_filter = ("topic", "handled", "created_at")
    search_fields = ("name", "email", "company", "message")
    list_editable = ("handled",)
    readonly_fields = ("created_at", "ip_address", "source_page")
