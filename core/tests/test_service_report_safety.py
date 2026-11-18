"""Service reports display source text without promoting it into active HTML."""
from html import unescape
from html.parser import HTMLParser

from django.test import SimpleTestCase

from core.utils import generate_service_report_html


class ReportMarkupSafetyTests(SimpleTestCase):
    def test_source_filename_response_query_and_metadata_are_text_not_active_markup(self):
        payload = '</pre><script>alert("source")</script><img src=x onerror=alert(1)>'
        report = generate_service_report_html(
            service_name=payload, service_id=payload, vertical=payload,
            response_data={"source": payload}, input_files=[{"filename": payload, "file_id": payload}],
            query=payload, additional_metadata={payload: payload}, execution_time_seconds=0.1)
        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
                self.attrs = []
            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                self.attrs.extend(attrs)
        parser = Tags()
        parser.feed(report)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in parser.attrs))
        self.assertIn(payload, unescape(report))
        self.assertIn("&lt;/pre&gt;&lt;script&gt;", report)
        self.assertIn('class="results-data"', report)

    def test_plain_text_response_is_also_escaped(self):
        report = generate_service_report_html("Analysis", "structure", "Legal", "<svg onload=alert(1)>")
        self.assertIn("&lt;svg onload=alert(1)&gt;", report)
        self.assertNotIn("<svg", report)
