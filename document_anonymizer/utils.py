import os
import sys
import json
import re
import logging
from django.conf import settings
from docx import Document
import spacy
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from unstructured.partition.pdf import partition_pdf
from document_anonymizer.models import Anonymize

from document_anonymizer.pii_taxonomy import (
    NON_PII_ENTITY_TYPES,
    PII_SCORE_THRESHOLD,
    SPACY_PII_LABELS,
    canonical_label,
    collect_pii_entries,
    is_plausible_pii_value,
    label_of_mask,
    score_entries,
    weight_for_label,
)
import sys
from django.db.models import Avg, Count, Q
from document_anonymizer.models import AnonymizationRun, Anonymize, DeAnonymize

from functools import lru_cache

logger = logging.getLogger(__name__)



def _skip_heavy_init() -> bool:
    if os.getenv("SKIP_NLP_INIT") == "1":
        return True
    # avoid heavy init during management commands
    mgmt_cmds = {"collectstatic", "migrate", "makemigrations", "check", "createsuperuser"}
    return any(cmd in sys.argv for cmd in mgmt_cmds)



class AnonymizationService:
    """
    Handles document extraction, full Presidio ➔ SpaCy anonymization pipeline, and de-anonymization.
    Now supports structured PDF extraction using unstructured.
    """


    def __init__(self):
        logger.info("📦 Initializing AnonymizationService with shared models...")
        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()

        self._spacy_model = os.getenv("SPACY_MODEL", "en_core_web_lg")
        self.nlp = None
        self._presidio_entities = None

        if _skip_heavy_init():
            logger.info(f"⏭️ Skipping spaCy load (SPACY_MODEL={self._spacy_model}) due to build/management command.")
        else:
            self._ensure_nlp_loaded()


    def _ensure_nlp_loaded(self):
        if self.nlp is not None:
            return
        try:
            self.nlp = spacy.load(self._spacy_model)
            logger.info(f"✅ spaCy model loaded: {self._spacy_model}")
        except OSError as e:
            logger.error(
                f"❌ spaCy model '{self._spacy_model}' not installed. "
                f"Install it in the image or set SPACY_MODEL to an installed model."
            )
            raise


    ''' 
    def __init__(self):
        logger.info("📦 Initializing AnonymizationService with shared models...")
        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()
        self.nlp = spacy.load("en_core_web_lg")  # Load once per task
    '''


    def extract_text_from_file(self, file_path):
        logger.info(f"🔄 Extracting text from: {file_path}")

        if not os.path.exists(file_path):
            logger.error(f"❌ File not found: {file_path}")
            return None, [], []

        try:
            if file_path.lower().endswith(".pdf"):
                return self.extract_text_from_pdf(file_path)
            elif file_path.lower().endswith(".txt"):
                with open(file_path, "r", encoding="utf-8") as f:
                    return f.read(), [], []
            elif file_path.lower().endswith(".docx"):
                return self.extract_text_from_docx(file_path), [], []
            else:
                # Fallback: try reading as plain UTF-8 text for any text-based format
                logger.info(f"📄 Attempting plain-text read for: {file_path}")
                with open(file_path, "r", encoding="utf-8") as f:
                    return f.read(), [], []
        except Exception as e:
            logger.error(f"❌ Failed to extract text from {file_path}: {e}")
            return None, [], []

    def extract_structured_text(self, file_path):
        """
        Dynamically select the correct structured extraction function based on file type.
        """
        logger.info(f"📄 extract_structured_text() ➔ Processing {file_path}")
        try:
            if file_path.lower().endswith(".pdf"):
                from unstructured.partition.pdf import partition_pdf
                elements = partition_pdf(filename=file_path)
            elif file_path.lower().endswith(".docx"):
                from unstructured.partition.docx import partition_docx
                elements = partition_docx(filename=file_path)
            else:
                # Fallback: treat as plain text for any text-based format
                logger.info(f"📄 Attempting plain-text structured read for: {file_path}")
                with open(file_path, "r", encoding="utf-8") as f:
                    text = f.read()
                return text, [], [{"element_id": "raw-0", "type": "Text", "text": text}]

            structured_text = "\n\n".join(el.text.strip() for el in elements if el.text and el.text.strip())
            return structured_text, elements, [el.to_dict() for el in elements]
        except Exception as e:
            logger.error(f"❌ extract_structured_text() failed: {e}")
            return None, [], []


    def extract_text_from_pdf(self, file_path):
        try:
            logger.info(f"📄 Using unstructured to extract structured PDF text from: {file_path}")
            elements = partition_pdf(filename=file_path)
            structured_text = "\n\n".join(el.text.strip() for el in elements if el.text and el.text.strip())

            return structured_text, elements, [el.to_dict() for el in elements]
        except Exception as e:
            logger.error(f"❌ Structured PDF extraction failed via unstructured: {e}")
            return None, [], []

    def extract_text_from_docx(self, file_path):
        try:
            doc = Document(file_path)
            return "\n".join([para.text for para in doc.paragraphs])
        except Exception as e:
            logger.error(f"❌ DOCX text extraction failed: {e}")
            return None
    
    def _get_presidio_pii_entities(self):
        """
        Presidio allow-list: every supported entity EXCEPT known non-PII types
        (dates, quantities, numbering...). Computed once per service instance.
        """
        if self._presidio_entities is None:
            supported = set(self.analyzer.get_supported_entities())
            self._presidio_entities = sorted(supported - NON_PII_ENTITY_TYPES)
        return self._presidio_entities

    def anonymize_text(self, text):
        """
        Anonymizes the input text using both Presidio and SpaCy engines.

        PII policy (GDPR Art.4 / NIST SP 800-122):
          - Presidio runs with a PII-only entity allow-list and a confidence
            floor, so weak regex hits ('R2' as a driver license) are dropped.
          - spaCy NER runs on the ORIGINAL text restricted to person/org/
            location labels, so Presidio masks are never re-detected as
            entities and numbering/dates/percentages are never masked.
          - ORG is deliberately kept (business-context redaction).
        Reuses shared engine instances for performance and memory efficiency.
        """
        logger.info("🔄 Running Presidio anonymization...")

        self._ensure_nlp_loaded()

        candidates = []

        # ---- Presidio pass on the ORIGINAL text --------------------------
        try:
            presidio_results = self.analyzer.analyze(
                text=text,
                entities=self._get_presidio_pii_entities(),
                language="en",
                score_threshold=PII_SCORE_THRESHOLD,
            )
        except TypeError:
            # Older presidio versions without score_threshold kwarg.
            presidio_results = [
                r for r in self.analyzer.analyze(
                    text=text,
                    entities=self._get_presidio_pii_entities(),
                    language="en",
                )
                if r.score >= PII_SCORE_THRESHOLD
            ]

        for result in presidio_results:
            value = text[result.start:result.end].strip()
            if not is_plausible_pii_value(value, result.entity_type):
                continue
            candidates.append({
                "start": result.start,
                "end": result.end,
                "label": canonical_label(result.entity_type),
                "value": value,
                "engine": "presidio",
                "rank": 0,
            })

        # ---- spaCy pass on the ORIGINAL text -----------------------------
        if self.nlp is not None:
            logger.info("🔄 Running SpaCy anonymization...")
            doc = self.nlp(text)
            for ent in doc.ents:
                if ent.label_ not in SPACY_PII_LABELS:
                    continue
                value = ent.text.strip()
                if not is_plausible_pii_value(value, ent.label_):
                    continue
                candidates.append({
                    "start": ent.start_char,
                    "end": ent.end_char,
                    "label": canonical_label(ent.label_),
                    "value": value,
                    "engine": "spacy",
                    "rank": 1,
                })

        # ---- resolve overlaps: earliest span wins; engine rank/length break ties
        candidates.sort(key=lambda c: (c["start"], c["rank"], -(c["end"] - c["start"])))
        spans = []
        last_end = -1
        for c in candidates:
            if c["start"] >= last_end:
                spans.append(c)
                last_end = c["end"]

        # ---- apply masks right-to-left to keep offsets valid --------------
        counters = {}
        presidio_map = {}
        spacy_map = {}
        final_masked = text

        for span in sorted(spans, key=lambda s: s["start"], reverse=True):
            label = span["label"]
            counters[label] = counters.get(label, 0) + 1
            mask = f"{label}_MASKED_{counters[label]}"
            final_masked = final_masked[:span["start"]] + mask + final_masked[span["end"]:]
            target = presidio_map if span["engine"] == "presidio" else spacy_map
            target[mask] = span["value"]

        combined_map = {**spacy_map, **presidio_map}

        logger.info(f"✅ Presidio map: {json.dumps(presidio_map, indent=2)}")
        logger.info(f"✅ SpaCy map: {json.dumps(spacy_map, indent=2)}")

        return final_masked, combined_map, presidio_map, spacy_map


    def deanonymize_pipeline(self, masked_text, spacy_mapping, presidio_mapping):
        partially_restored = self.reverse_masking(masked_text, spacy_mapping)
        fully_restored = self.reverse_masking(partially_restored, presidio_mapping)
        return fully_restored


    def reverse_masking(self, text, entity_mapping):
        for mask, original in sorted(entity_mapping.items(), key=lambda x: len(x[0]), reverse=True):
            pattern = re.compile(r'\b' + re.escape(mask) + r'\b')
            text = pattern.sub(original, text)
        return text


    def get_file_path(self, original_file_path, folder="anonymized", extension="txt"):
        directory = os.path.join(os.path.dirname(original_file_path), folder)
        os.makedirs(directory, exist_ok=True)
        filename = os.path.basename(original_file_path).replace(".pdf", f".{extension}")
        return os.path.join(directory, filename)

    def update_block_text(self, structured_blocks, element_id, new_text):
        for block in structured_blocks:
            if block.get("element_id") == element_id:
                block["text"] = new_text
                return True
        return False


    def compute_risk_score(self, original_text: str, presidio_map: dict, spacy_map: dict):
        """
        Realistic PII risk scoring.

        Only genuine PII is counted (GDPR Art.4 / NIST SP 800-122):
          - non-PII labels (dates, numbering, percentages, products, laws...)
            are excluded entirely;
          - junk/mask-polluted values are filtered out;
          - each distinct value counts ONCE even when tagged by both engines
            or under several ID labels ('237971098' = one identifier, not three).

        Returns:
          {
            "risk_score": float,                          # 0..100 logistic
            "risk_level": "Ok" | "Low" | "Medium" | "High",
            "breakdown": { label: count },                # de-duplicated true PII
            "contributions": { label: raw_contribution }
          }
        """
        text = (original_text or "").strip()
        doc_tokens = len(re.findall(r"\w+", text))
        entries = collect_pii_entries(
            presidio_map=presidio_map,
            spacy_map=spacy_map,
        )
        return score_entries(entries, doc_tokens=doc_tokens)


def generate_anonymized_html(masked_text, entity_mapping):
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 40px;
            background-color: #f7f9fc;
            color: #2e2e2e;
            line-height: 1.7;
        }
        .masked {
            background-color: #fff3cd;
            color: #856404;
            font-weight: bold;
            padding: 2px 6px;
            border-radius: 4px;
            border: 1px solid #ffeeba;
            cursor: help;
        }
        pre {
            background-color: #ffffff;
            padding: 20px;
            border: 1px solid #ddd;
            border-radius: 6px;
            overflow-x: auto;
            white-space: pre-wrap;
            word-break: break-word;
        }
    </style>
</head>
<body>
    <pre>{text}</pre>
</body>
</html>
"""
    highlighted_text = masked_text
    for mask, original in sorted(entity_mapping.items(), key=lambda x: len(x[0]), reverse=True):
        tooltip = f"title='Original: {original}'"
        highlighted_text = highlighted_text.replace(
            mask, f'<span class="masked" {tooltip}>{mask}</span>'
        )

    return html_template.replace("{text}", highlighted_text)

from docx import Document as DocxDocument

def summarize_blocks(blocks, element_ids, model="gpt-4"):
    """
    Dummy implementation for now. Replace with actual GPT-4 call.
    """
    results = []
    for block in blocks:
        if block.get("element_id") in element_ids:
            text = block.get("text", "")
            results.append({
                "element_id": block.get("element_id"),
                "summary": f"Summary of: {text[:60]}..."
            })
    return results


def update_structured_block(json_path, element_id, new_text):
    with open(json_path, "r", encoding="utf-8") as f:
        blocks = json.load(f)

    updated = False
    for block in blocks:
        if block.get("element_id") == element_id:
            block["text"] = new_text
            updated = True
            break

    if updated:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(blocks, f, indent=2)

    return updated


def export_to_markdown(json_path, generate_preview=False):
    """
    Converts structured JSON blocks into a well-formatted Markdown document.
    - Adds a TOC (Table of Contents) based on Titles and Headers.
    - Formats tables and content semantically.
    - Optionally saves a plain-text preview for quick viewing.
    """
    import hashlib

    md_lines = []
    toc_lines = ["## 📚 Table of Contents\n"]

    with open(json_path, "r", encoding="utf-8") as f:
        blocks = json.load(f)

    for i, block in enumerate(blocks):
        block_type = block.get("type", "Block")
        text = block.get("text", "").strip()

        # Generate an anchor-friendly heading ID
        hash_id = hashlib.md5(text.encode("utf-8")).hexdigest()[:6]

        if block_type.lower() in ["title", "header"]:
            toc_lines.append(f"- [{text}](#{hash_id})")
            md_lines.append(f"### <a name=\"{hash_id}\"></a>{text}\n")

        elif block_type.lower() in ["listitem", "unorderedlist", "orderedlist"]:
            items = text.split("\n")
            for item in items:
                md_lines.append(f"- {item.strip()}")
            md_lines.append("")  # Spacer

        elif block_type.lower() in ["table", "tabular"]:
            # Handle as Markdown table
            rows = [line for line in text.split("\n") if line.strip()]
            if len(rows) >= 2:
                header = rows[0].split()
                separator = ["---"] * len(header)
                md_lines.append("| " + " | ".join(header) + " |")
                md_lines.append("| " + " | ".join(separator) + " |")
                for row in rows[1:]:
                    cols = row.split()
                    md_lines.append("| " + " | ".join(cols) + " |")
            else:
                md_lines.append(f"```table\n{text}\n```")

        elif block_type.lower() == "narrativetext":
            md_lines.append(f"> {text}")

        else:
            md_lines.append(f"**{block_type}**\n\n{text}")

        md_lines.append("\n---\n")

    toc_text = "\n".join(toc_lines) + "\n---\n"
    full_md = toc_text + "\n".join(md_lines)

    md_path = json_path.replace(".json", ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(full_md)

    # Optional: Plain-text preview (no Markdown formatting)
    if generate_preview:
        preview_text = "\n".join([block.get("text", "").strip() for block in blocks])
        preview_path = json_path.replace(".json", "_preview.txt")
        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(preview_text)

    return md_path


def export_to_docx(json_path):
    doc = DocxDocument()
    with open(json_path, "r", encoding="utf-8") as f:
        blocks = json.load(f)

    for block in blocks:
        doc.add_heading(block.get("type", "Block"), level=2)
        doc.add_paragraph(block.get("text", ""))

    out_path = json_path.replace(".json", ".docx")
    doc.save(out_path)
    return out_path


def compute_global_anonymization_stats(
    client_name=None,
    project_id=None,
    service_id=None,
    date_from=None,
    date_to=None
):
    """
    Computes anonymization stats, optionally filtered by client/project/service/date.
    """

    from django.db.models import Q

    filters = Q(is_active=True)

    if client_name:
        filters &= Q(run__client_name=client_name)
    if project_id:
        filters &= Q(run__project_id=project_id)
    if service_id:
        filters &= Q(run__service_id=service_id)
    if date_from:
        filters &= Q(updated_at__date__gte=date_from)
    if date_to:
        filters &= Q(updated_at__date__lte=date_to)

    queryset = Anonymize.objects.filter(filters)

    files_with_entities = 0
    files_without_entities = 0
    total_entities_anonymized = 0
    entity_type_breakdown = {}

    for record in queryset:
        # De-duplicated, PII-only counting (same policy as the risk scorer)
        entries = collect_pii_entries(
            presidio_map=record.presidio_masking_map,
            spacy_map=record.spacy_masking_map,
        )

        entity_counts = {}
        for entry in entries.values():
            label = entry["label"]
            entity_counts[label] = entity_counts.get(label, 0) + entry["count"]

        file_entity_total = sum(entity_counts.values())
        total_entities_anonymized += file_entity_total

        for entity_type, count in entity_counts.items():
            entity_type_breakdown[entity_type] = (
                entity_type_breakdown.get(entity_type, 0) + count
            )

        if file_entity_total > 0:
            files_with_entities += 1
        else:
            files_without_entities += 1

    result = {
        "files_with_entities": files_with_entities,
        "files_without_entities": files_without_entities,
        "total_entities_anonymized": total_entities_anonymized,
        "entity_type_breakdown": entity_type_breakdown,
    }

    return result


def calculate_anonymization_insights(user):
    insights = {}

    # Runs by status
    runs = AnonymizationRun.objects.filter(client_name=user.username or user.email)
    status_counts = runs.values("status").annotate(count=Count("id"))
    insights["anonymization_status_counts"] = list(status_counts)

    # Average duration
    durations = []
    for run in runs:
        if run.updated_at and run.created_at:
            delta = (run.updated_at - run.created_at).total_seconds()
            durations.append(delta)
    insights["average_anonymization_time_seconds"] = sum(durations)/len(durations) if durations else 0

    # Risk levels
    risk_counts = (
        Anonymize.objects.filter(original_file__user=user)
        .values("risk_level")
        .annotate(count=Count("id"))
    )
    insights["risk_level_distribution"] = list(risk_counts)

    # High-risk documents
    high_risk_files = (
        Anonymize.objects
        .filter(original_file__user=user, risk_level="High")
        .values("original_file__filename", "risk_score")
        .order_by("-risk_score")[:10]
    )
    insights["top_high_risk_files"] = list(high_risk_files)

    # De-anonymization count
    deanonymize_count = (
        DeAnonymize.objects.filter(file__user=user).count()
    )
    insights["deanonymization_count"] = deanonymize_count

    # Documents bucketed by risk level.
    # bucket_counts: exact totals computed DB-side (scales to any corpus size).
    # risk_buckets: small preview list per bucket so the UI can render
    # immediately; full lists are fetched on demand via the paginated
    # /anonymizer/risk-bucket-documents/ endpoint.
    risk_levels = ["High", "Medium", "Low", "Ok"]
    base_qs = Anonymize.objects.filter(
        original_file__user=user, is_active=True, status="Completed"
    )
    counts_rows = (
        base_qs.values("risk_level").annotate(count=Count("id"))
    )
    raw_counts = {r["risk_level"]: r["count"] for r in counts_rows}
    insights["bucket_counts"] = {
        level: int(raw_counts.get(level, 0)) for level in risk_levels
    }

    risk_buckets = {level: [] for level in risk_levels}
    for row in (
        base_qs.values(
            "id",
            "original_file_id",
            "original_file__filename",
            "risk_score",
            "risk_level",
            "updated_at",
        ).order_by("-risk_score")[:20]
    ):
        level = row.get("risk_level") if row.get("risk_level") in risk_buckets else "Ok"
        if len(risk_buckets[level]) >= 5:
            continue
        risk_buckets[level].append({
            "anonymize_id": str(row["id"]),
            "file_id": row["original_file_id"],
            "filename": row["original_file__filename"],
            "risk_score": row["risk_score"],
            "risk_level": level,
            "updated_at": row["updated_at"],
        })
    insights["risk_buckets"] = risk_buckets

    # Aggregate entity stats across all anonymized files for this user
    anonymize_records = Anonymize.objects.filter(original_file__user=user, is_active=True)
    files_with_entities = 0
    files_without_entities = 0
    total_entities_anonymized = 0
    entity_type_breakdown = {}

    for record in anonymize_records:
        # De-duplicated, PII-only counting (same policy as the risk scorer)
        entries = collect_pii_entries(
            presidio_map=record.presidio_masking_map,
            spacy_map=record.spacy_masking_map,
        )

        entity_counts = {}
        for entry in entries.values():
            label = entry["label"]
            entity_counts[label] = entity_counts.get(label, 0) + entry["count"]

        file_entity_total = sum(entity_counts.values())
        total_entities_anonymized += file_entity_total

        for entity_type, count in entity_counts.items():
            entity_type_breakdown[entity_type] = (
                entity_type_breakdown.get(entity_type, 0) + count
            )

        if file_entity_total > 0:
            files_with_entities += 1
        else:
            files_without_entities += 1

    insights["files_with_entities"] = files_with_entities
    insights["files_without_entities"] = files_without_entities
    insights["total_entities_anonymized"] = total_entities_anonymized
    insights["entity_type_breakdown"] = entity_type_breakdown

    return insights



# Reuse one heavy service (spaCy + Presidio) per worker
@lru_cache(maxsize=1)
def get_shared_anonymization_service():
    return AnonymizationService()

def list_supported_entities_runtime():
    """
    Returns the actual label sets available at runtime.
    Adds model/pipeline metadata, and avoids re-initializing heavy models.
    """
    data = {
        "spacy": [],
        "presidio": [],
        "meta": {"spacy_model": None, "spacy_pipes": []}
    }
    try:
        svc = get_shared_anonymization_service()

        # spaCy
        data["meta"]["spacy_model"] = getattr(getattr(svc, "nlp", None), "meta", {}).get("name")
        data["meta"]["spacy_pipes"] = list(getattr(svc.nlp, "pipe_names", []))
        if "ner" in getattr(svc.nlp, "pipe_names", []):
            data["spacy"] = sorted(set(svc.nlp.get_pipe("ner").labels))

        # Presidio
        data["presidio"] = sorted(set(svc.analyzer.get_supported_entities()))
    except Exception as e:
        logger.warning(f"[entities] Introspection failed: {e}")

    return data

# --- Optional: expose the risk weights the scorer uses (handy for docs/UI) ---
def risk_weight_for_label(label: str) -> float:
    """
    Returns the numeric weight used for a given (possibly non-canonical) label.
    Kept in sync automatically via the shared pii_taxonomy policy module.
    """
    return weight_for_label(label)

def list_supported_entities_with_weights():
    """
    Returns { label: weight } for all labels currently supported by either engine.
    """
    data = list_supported_entities_runtime()
    labels = sorted(set(data.get("spacy", [])) | set(data.get("presidio", [])))
    return {lbl: risk_weight_for_label(lbl) for lbl in labels}

