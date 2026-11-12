from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Enquiry

URL = "/api/v1/enquiries/"
VALID = {
    "name": "Jane Doe", "email": "jane@firm.test", "company": "Harper & Lowe",
    "role": "Partner", "topic": "demo", "practice_area": "Private equity",
    "message": "We review 400 contracts per deal.", "consent": True, "source_page": "/contact_us",
}


@override_settings(REST_FRAMEWORK={
    "DEFAULT_THROTTLE_RATES": {"anon": "1000/hour"},
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
})
class EnquiryTests(TestCase):
    def test_valid_enquiry_is_stored_without_login(self):
        r = APIClient().post(URL, VALID, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        e = Enquiry.objects.get()
        self.assertEqual(e.email, "jane@firm.test")
        self.assertTrue(e.consent)

    def test_consent_is_required(self):
        r = APIClient().post(URL, {**VALID, "consent": False}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Enquiry.objects.exists())

    def test_invalid_email_rejected(self):
        r = APIClient().post(URL, {**VALID, "email": "not-an-email"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_honeypot_submissions_are_dropped_silently(self):
        r = APIClient().post(URL, {**VALID, "website": "http://spam.example"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertFalse(Enquiry.objects.exists())
