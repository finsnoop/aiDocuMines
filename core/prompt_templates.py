"""
Assistant prompts for catalog services, used by the "Services & Tools" list in the
Assistant. Specific prompts for common work, a sound default for everything else.
Every prompt asks for source references so answers can be checked.
"""

CITE = " For every point, quote or reference the page or clause it comes from. If the document does not say, write 'Not stated'."

BY_KEYWORD = [
    ("risk", "Review this document for legal and commercial risk. List each risk clause (change of control, "
             "assignment, exclusivity, termination, liability caps, indemnities, non-compete) in a table with "
             "columns: Clause, What it says, Risk level (High/Medium/Low), Why it matters."),
    ("issue", "Spot the legal issues in this document. For each issue give: the issue, the relevant text, "
              "the likely consequence and a recommended next step."),
    ("finding", "Draft due-diligence findings for this document: key terms, red flags and recommended "
                "actions, grouped by severity."),
    ("classif", "Classify this document: document type, parties, date, governing law and subject matter. "
                "Return a short table."),
    ("damages", "Extract every amount claimed or awarded (damages, costs, interest), with the date, the party "
                "and the basis for each. Present a table and a total."),
    ("evidence", "Assess this document as evidence: what it proves, who it involves, key dates and how "
                 "relevant it is to the claims (High/Medium/Low)."),
    ("wage", "Extract hours worked, overtime, pay rates, deductions and pay periods into a table, and flag "
             "anything that looks inconsistent with minimum wage or overtime rules."),
    ("communic", "Review these communications for employment-law issues (harassment, discrimination, "
                 "retaliation, policy breaches). List each item with date, people involved and severity."),
    ("policy", "Compare this policy against good practice and applicable law. List gaps, the risk each "
               "creates and suggested wording to close it."),
    ("patent", "Summarise this patent: claims (independent first), key technical features, priority date "
               "and the scope a competitor would need to avoid."),
    ("infring", "Compare the described product or process against the patent claims, element by element, "
                "and give a view on likely infringement for each claim."),
    ("prior", "Identify potential prior art themes for this invention and the claim elements each would "
              "challenge."),
    ("dsar", "Find all personal information about the data subject in this document, grouped by category "
             "(identity, contact, financial, health, opinions), and note what should be redacted before "
             "disclosure and why."),
    ("pii", "Find all personal information (names, ID numbers, contact details, account numbers, health data) "
            "and list it by type with its location."),
    ("redact", "List everything that should be redacted from this document before it is shared externally, "
               "with the reason for each."),
    ("gap", "Run a compliance gap analysis: list each requirement, whether the document meets it "
            "(Met/Partly/Not met) and what is missing."),
    ("anonym", "Identify the personal and confidential details that must be anonymised in this document."),
    ("translat", "Translate the key passages of this document into English and summarise them."),
    ("ocr", "Summarise the text of this scanned document and list any passages that are unclear or illegible."),
    ("structur", "Outline the structure of this document: sections, schedules, defined terms and cross-references."),
    ("redlin", "Summarise the changes between versions of this document and their legal effect."),
    ("metadata", "Extract the key metadata: title, parties, dates, reference numbers, governing law and value."),
    ("search", "Find the passages in this document most relevant to my question and explain each one."),
    ("cluster", "Group the topics in this document and summarise each group."),
    ("summary", "Give a 5-bullet executive summary of this document followed by the three most important "
                "points a partner should know."),
    ("invoice", "Extract invoice data: supplier, invoice number, dates, line items, amounts, tax and total, "
                "and flag anything unusual."),
    ("quote", "Extract the pricing, scope, assumptions, exclusions and validity of this quote or bid."),
]


def prompt_for(service: dict) -> str:
    """Prompt for a catalog service entry (matched on service_type and display name)."""
    key = f"{service.get('service_type', '')} {service.get('display_name', '')}".lower()
    for word, prompt in BY_KEYWORD:
        if word in key:
            return prompt + CITE
    name = service.get("display_name") or service.get("service_type") or "this review"
    desc = (service.get("description") or "").rstrip(".")
    task = f"{desc[0].lower()}{desc[1:]}" if desc else f"carry out {name}"
    return f"Using this document, {task}. Present the result as a clear table or list." + CITE
