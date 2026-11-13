"""
Shared PII classification policy for the document anonymizer.

Grounded in:
- GDPR Art. 4(1): personal data relates to an identified/identifiable NATURAL
  person; legal-entity data is out of scope; IPs are personal data (CJEU Breyer).
- NIST SP 800-122: PII distinguishes or traces an individual alone or combined
  with other information; bare numbers/dates are quasi-identifiers only when
  linked to a person.

Stdlib-only so it can be unit-tested and reused by Celery workers,
management commands and stats endpoints without loading Django.
"""

import math
import re
from collections import OrderedDict

# --------------------------------------------------------------- taxonomy --

# Entity labels that are NOT personal data on their own: they must neither be
# masked nor counted toward risk scores.
NON_PII_ENTITY_TYPES = {
    # numbering & quantities: "1", "two", "12.4", "50%", "8 hours"
    "CARDINAL", "ORDINAL", "QUANTITY", "PERCENT", "MONEY", "TIME",
    # standalone dates/times: quasi-identifiers only when tied to a person;
    # document dates, durations and section numbers are noise.
    "DATE", "DATE_TIME",
    # references that do not identify a natural person
    "LANGUAGE", "EVENT", "WORK_OF_ART", "LAW", "PRODUCT", "FAC",
}

# spaCy NER labels retained for masking/scoring.
# ORG is kept deliberately (business-context redaction) even though GDPR
# strictly covers natural persons only.
SPACY_PII_LABELS = {"PERSON", "ORG", "GPE", "LOC", "LOCATION", "NORP"}

# Minimum confidence for a Presidio detection to be accepted.
PII_SCORE_THRESHOLD = 0.40

_LABEL_ALIASES = {"PER": "PERSON", "LOC": "LOC", "ORG": "ORG"}


def canonical_label(label):
    L = (label or "").upper().strip()
    return _LABEL_ALIASES.get(L, L)


def label_of_mask(mask_key):
    """'US_SSN_MASKED_3' -> 'US_SSN'."""
    return canonical_label((mask_key or "").split("_MASKED_")[0])


# ------------------------------------------------------------ plausibility --

# Labels whose values are expected to be purely numeric identifiers.
NUMERIC_ID_LABELS = {
    "CREDIT_CARD", "CRYPTO", "IBAN_CODE", "US_BANK_NUMBER",
    "US_SSN", "US_ITIN", "US_PASSPORT", "US_DRIVER_LICENSE",
    "UK_NINO", "UK_NHS", "ES_NIF", "ES_NIE",
    "IT_FISCAL_CODE", "IT_DRIVER_LICENSE", "IT_VAT_CODE", "IT_PASSPORT",
    "IT_IDENTITY_CARD", "PL_PESEL", "SG_NRIC_FIN", "SG_UEN",
    "AU_ABN", "AU_ACN", "AU_TFN", "AU_MEDICARE",
    "IN_PAN", "IN_AADHAAR", "IN_VEHICLE_REGISTRATION", "IN_VOTER",
    "IN_PASSPORT", "FI_PERSONAL_IDENTITY_CODE", "KR_RRN", "TH_TNIN",
    "MEDICAL_LICENSE",
}

# Numeric-looking values are legitimate under these labels as well.
NUMERIC_ALLOWED_LABELS = NUMERIC_ID_LABELS | {
    "PHONE_NUMBER", "IP_ADDRESS", "MAC_ADDRESS", "US_ZIP_CODE",
}

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def is_mask_polluted(value):
    return "_MASKED_" in (value or "")


def is_plausible_pii_value(value, label):
    """
    Second-stage validation applied AFTER engine detection. Kills the
    false-positive families observed in production:
      - mask tokens re-detected as entities ("PERSON_MASKED_1")
      - version strings / short codes sold as IDs ("R2", "X3", "A100")
      - bare numbers under word-labels (numbering caught by generic NER)
      - IPv4 literals mis-labelled as PHONE_NUMBER
    """
    v = (value or "").strip()
    if not v or is_mask_polluted(v):
        return False
    L = canonical_label(label)
    compact = re.sub(r"\s+", "", v)
    digits = re.sub(r"\D", "", v)

    # An IPv4 literal is only an online identifier under IP_ADDRESS.
    if _IPV4_RE.match(v) and L != "IP_ADDRESS":
        return False

    if L in NUMERIC_ALLOWED_LABELS:
        # Real numeric identifiers carry enough digits within sane bounds.
        return len(digits) >= 5 and 4 <= len(compact) <= 48

    # Word-like PII must contain letters and not be a tiny fragment.
    return bool(re.search(r"[A-Za-z]", v)) and len(compact) >= 3


# ----------------------------------------------------------------- weights --

HIGH = {
    "CREDIT_CARD", "US_BANK_NUMBER", "IBAN_CODE", "CRYPTO",
    "US_SSN", "US_ITIN", "US_PASSPORT", "US_DRIVER_LICENSE",
    "UK_NINO", "UK_NHS",
    "ES_NIF", "ES_NIE",
    "IT_FISCAL_CODE", "IT_DRIVER_LICENSE", "IT_VAT_CODE", "IT_PASSPORT",
    "IT_IDENTITY_CARD",
    "PL_PESEL",
    "SG_NRIC_FIN", "SG_UEN",
    "AU_ABN", "AU_ACN", "AU_TFN", "AU_MEDICARE",
    "IN_PAN", "IN_AADHAAR", "IN_VEHICLE_REGISTRATION", "IN_VOTER", "IN_PASSPORT",
    "FI_PERSONAL_IDENTITY_CODE", "KR_RRN", "TH_TNIN",
}
MED_HIGH = {"MEDICAL_LICENSE"}
MED = {"EMAIL_ADDRESS", "PHONE_NUMBER", "IP_ADDRESS", "URL", "NRP", "NORP"}
LOW = {
    "PERSON", "GPE", "LOC", "ORG", "LOCATION",
}

_ID_HINT_RE = re.compile(
    r"(SSN|PASSPORT|DRIVER|LICENSE|NINO|NRIC|PESEL|TFN|AADHAAR|RRR|RRN|TNIN|"
    r"IBAN|BANK|ACCOUNT|CREDIT|CARD|CRYPTO|FISCAL|VAT)"
)
_CONTACT_HINT_RE = re.compile(r"(EMAIL|PHONE|MOBILE|TEL|IP|URL|DOMAIN)")


def weight_for_label(label):
    L = canonical_label(label)
    if L in HIGH:
        return 10.0
    if L in MED_HIGH:
        return 6.0
    if L in MED:
        return 4.0
    if L in LOW:
        return 2.0
    if _ID_HINT_RE.search(L):
        return 7.0
    if _CONTACT_HINT_RE.search(L):
        return 4.0
    return 2.0


def saturation_k_for_label(label):
    """Diminishing-returns rate: smaller k saturates sooner (first hit counts most)."""
    w = weight_for_label(label)
    if w >= 10.0:
        return 1.0
    if w >= 6.0:
        return 1.5
    if w >= 4.0:
        return 2.0
    if w >= 2.0:
        return 3.0
    return 4.0


# ------------------------------------------------------------ dedupe + score -

def collect_pii_entries(presidio_map=None, spacy_map=None):
    """
    Merge engine masking maps into de-duplicated PII entries.

    Each distinct normalized value is counted ONCE, attributed to its
    highest-severity label ('237971098' tagged US_SSN+US_PASSPORT+US_BANK_NUMBER
    counts as a single identifier, not three).
    """
    best = {}
    order = []
    for mapping in (presidio_map, spacy_map):
        for mask, val in (mapping or {}).items():
            label = label_of_mask(mask)
            if label in NON_PII_ENTITY_TYPES:
                continue
            v = (val or "").strip()
            if not is_plausible_pii_value(v, label):
                continue
            key = re.sub(r"\s+", " ", v).strip().lower()
            if not key:
                continue
            w = weight_for_label(label)
            if key not in best:
                best[key] = {"label": label, "value": v, "count": 0, "weight": w}
                order.append(key)
            elif w > best[key]["weight"]:
                best[key].update({"label": label, "weight": w})
            best[key]["count"] += 1
    return OrderedDict((k, best[k]) for k in order)


def score_entries(entries, doc_tokens=0):
    """
    Severity-weighted risk score in 0..100 with diminishing returns per label.

    Calibration targets:
      - org/product names only          -> Ok / Low   (<35)
      - a few contact details           -> Low/Medium (~35)
      - national ID / financial number  -> High       (>70)
      - medical/legal doc with IDs+contacts -> High
    """
    label_counts = {}
    contributions = {}
    raw = 0.0
    for e in entries.values():
        label_counts[e["label"]] = label_counts.get(e["label"], 0) + e["count"]

    for label, n in label_counts.items():
        w = weight_for_label(label)
        k = saturation_k_for_label(label)
        contrib = w * (1.0 - math.exp(-float(n) / k))
        contributions[label] = round(contrib, 4)
        raw += contrib

    # Mild damping so very long documents don't inflate purely by volume.
    damping = min(1.0, math.sqrt(800.0 / max(int(doc_tokens or 0), 800)))
    raw *= damping

    alpha, beta = 0.40, 8.0
    risk_score = round(100.0 / (1.0 + math.exp(-alpha * (raw - beta))), 2)

    if risk_score >= 70.0:
        risk_level = "High"
    elif risk_score >= 35.0:
        risk_level = "Medium"
    elif risk_score > 0.0:
        risk_level = "Low"
    else:
        risk_level = "Ok"

    return {
        "risk_score": risk_score,
        "risk_level": risk_level,
        "breakdown": dict(sorted(label_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "contributions": dict(sorted(contributions.items(), key=lambda kv: -kv[1])),
    }
