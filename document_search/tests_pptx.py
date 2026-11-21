"""PowerPoint decks are indexed: slide text, tables and speaker notes are extracted, and .pptx uploads auto-index."""
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from document_search.utils import INDEXABLE_MIME_TYPES, extract_text


class PptxExtractionTests(SimpleTestCase):
    def make_deck(self, folder):
        from pptx import Presentation
        from pptx.util import Inches
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = "Project Falcon board update"
        slide.placeholders[1].text = "Completion expected Q3 2026"
        slide.notes_slide.notes_text_frame.text = "Mention the escrow of R12 million"
        table_slide = deck.slides.add_slide(deck.slide_layouts[5])
        table = table_slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
        table.cell(0, 0).text, table.cell(0, 1).text = "Risk", "Owner"
        table.cell(1, 0).text, table.cell(1, 1).text = "Change of control", "Legal"
        path = Path(folder) / "board.pptx"
        deck.save(path)
        return path

    def test_slide_text_tables_and_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            text = extract_text(self.make_deck(folder))
        self.assertIn("Slide 1:", text)
        self.assertIn("Project Falcon board update", text)
        self.assertIn("Completion expected Q3 2026", text)
        self.assertIn("Notes: Mention the escrow of R12 million", text)
        self.assertIn("Change of control | Legal", text)

    def test_broken_deck_does_not_raise(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.pptx"
            path.write_bytes(b"not a zip")
            self.assertNotIn("Slide 1:", extract_text(path))   # the generic plain-text fallback may apply

    def test_pptx_is_auto_indexed(self):
        self.assertIn("application/vnd.openxmlformats-officedocument.presentationml.presentation", INDEXABLE_MIME_TYPES)
