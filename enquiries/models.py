from django.db import models


class Enquiry(models.Model):
    """A demo request or contact message submitted from the public website."""

    TOPIC_CHOICES = [
        ("demo", "Book a demo"),
        ("pricing", "Pricing and commercial terms"),
        ("security", "Security and data protection"),
        ("partnership", "Partnership"),
        ("support", "Support for an existing account"),
        ("other", "Something else"),
    ]

    name = models.CharField(max_length=200)
    email = models.EmailField()
    company = models.CharField(max_length=200, blank=True)
    role = models.CharField(max_length=200, blank=True)
    topic = models.CharField(max_length=20, choices=TOPIC_CHOICES, default="demo")
    practice_area = models.CharField(max_length=120, blank=True)
    message = models.TextField(blank=True)
    consent = models.BooleanField(default=False, help_text="Agreed to be contacted about the enquiry")
    source_page = models.CharField(max_length=200, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    handled = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "enquiries"

    def __str__(self):
        return f"{self.get_topic_display()} from {self.name} <{self.email}>"
