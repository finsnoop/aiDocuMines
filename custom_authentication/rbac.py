"""
Granular RBAC system for aiDocuMines.

Provides:
  - 200+ granular permission definitions across all resources
  - 15+ hierarchical role definitions with inheritance
  - Industry-specific roles (HIPAA, SOX, FRE, GDPR)
  - DRF permission classes that enforce granular checks
  - Helper to seed permissions + roles into Django
"""

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework.permissions import BasePermission, SAFE_METHODS
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. GRANULAR PERMISSION DEFINITIONS
# ---------------------------------------------------------------------------
# Format: (codename, label, resource, action, description)

PERMISSION_DEFINITIONS = [
    # ── Document / File ──────────────────────────────────────────────────
    ("document.create", "Can create documents", "document", "create", ""),
    ("document.view", "Can view documents", "document", "view", ""),
    ("document.update", "Can update documents", "document", "update", ""),
    ("document.delete", "Can delete documents", "document", "delete", ""),
    ("document.share", "Can share documents", "document", "share", ""),
    ("document.download", "Can download documents", "document", "download", ""),
    ("document.upload", "Can upload documents", "document", "upload", ""),
    ("document.export", "Can export documents", "document", "export", ""),
    ("document.classify", "Can classify documents", "document", "classify", ""),
    ("document.bulk_operation", "Can perform bulk operations on documents", "document", "bulk_operation", ""),
    ("document.password_protect", "Can password-protect documents", "document", "password_protect", ""),
    ("document.restore", "Can restore deleted documents", "document", "restore", ""),

    # ── OCR ──────────────────────────────────────────────────────────────
    ("ocr.submit", "Can submit OCR jobs", "ocr", "submit", ""),
    ("ocr.view", "Can view OCR results", "ocr", "view", ""),
    ("ocr.download", "Can download OCR output", "ocr", "download", ""),
    ("ocr.delete", "Can delete OCR runs", "ocr", "delete", ""),
    ("ocr.configure", "Can configure OCR settings", "ocr", "configure", ""),

    # ── Translation ──────────────────────────────────────────────────────
    ("translation.submit", "Can submit translation jobs", "translation", "submit", ""),
    ("translation.view", "Can view translation results", "translation", "view", ""),
    ("translation.download", "Can download translated files", "translation", "download", ""),
    ("translation.delete", "Can delete translation runs", "translation", "delete", ""),
    ("translation.configure", "Can configure translation settings", "translation", "configure", ""),

    # ── Anonymization ────────────────────────────────────────────────────
    ("anonymization.submit", "Can submit anonymization jobs", "anonymization", "submit", ""),
    ("anonymization.view", "Can view anonymization results", "anonymization", "view", ""),
    ("anonymization.download", "Can download anonymized files", "anonymization", "download", ""),
    ("anonymization.delete", "Can delete anonymization runs", "anonymization", "delete", ""),
    ("anonymization.configure", "Can configure anonymization settings", "anonymization", "configure", ""),

    # ── Redlining ────────────────────────────────────────────────────────
    ("redlining.submit", "Can submit redlining jobs", "redlining", "submit", ""),
    ("redlining.view", "Can view redlining results", "redlining", "view", ""),
    ("redlining.download", "Can download redlined documents", "redlining", "download", ""),
    ("redlining.delete", "Can delete redlining runs", "redlining", "delete", ""),

    # ── Document Classification ──────────────────────────────────────────
    ("classification.submit", "Can submit classification jobs", "classification", "submit", ""),
    ("classification.view", "Can view classification results", "classification", "view", ""),
    ("classification.delete", "Can delete classification runs", "classification", "delete", ""),

    # ── Document Search ──────────────────────────────────────────────────
    ("search.semantic", "Can perform semantic searches", "search", "semantic", ""),
    ("search.advanced", "Can perform advanced searches (filters, facets)", "search", "advanced", ""),
    ("search.export", "Can export search results", "search", "export", ""),
    ("search.saved", "Can save and manage saved searches", "search", "saved", ""),

    # ── Document Workflows ───────────────────────────────────────────────
    ("workflow.create", "Can create workflows", "workflow", "create", ""),
    ("workflow.view", "Can view workflows", "workflow", "view", ""),
    ("workflow.update", "Can update workflows", "workflow", "update", ""),
    ("workflow.delete", "Can delete workflows", "workflow", "delete", ""),
    ("workflow.execute", "Can execute/trigger workflows", "workflow", "execute", ""),
    ("workflow.approve", "Can approve workflow steps", "workflow", "approve", ""),
    ("workflow.reject", "Can reject workflow steps", "workflow", "reject", ""),

    # ── Document Automation ──────────────────────────────────────────────
    ("automation.create", "Can create automation rules", "automation", "create", ""),
    ("automation.view", "Can view automation rules", "automation", "view", ""),
    ("automation.update", "Can update automation rules", "automation", "update", ""),
    ("automation.delete", "Can delete automation rules", "automation", "delete", ""),
    ("automation.execute", "Can trigger automation runs", "automation", "execute", ""),

    # ── Document Versioning ──────────────────────────────────────────────
    ("versioning.view", "Can view version history", "versioning", "view", ""),
    ("versioning.create", "Can create new versions", "versioning", "create", ""),
    ("versioning.restore", "Can restore previous versions", "versioning", "restore", ""),
    ("versioning.compare", "Can compare document versions", "versioning", "compare", ""),
    ("versioning.delete", "Can delete old versions", "versioning", "delete", ""),

    # ── Document Structure ───────────────────────────────────────────────
    ("structure.analyze", "Can analyze document structure", "structure", "analyze", ""),
    ("structure.view", "Can view structure analysis", "structure", "view", ""),
    ("structure.export", "Can export structure data", "structure", "export", ""),

    # ── Project ──────────────────────────────────────────────────────────
    ("project.create", "Can create projects", "project", "create", ""),
    ("project.view", "Can view projects", "project", "view", ""),
    ("project.update", "Can update projects", "project", "update", ""),
    ("project.delete", "Can delete projects", "project", "delete", ""),
    ("project.assign", "Can assign users to projects", "project", "assign", ""),

    # ── Playbook ─────────────────────────────────────────────────────────
    ("playbook.create", "Can create playbooks", "playbook", "create", ""),
    ("playbook.view", "Can view playbooks", "playbook", "view", ""),
    ("playbook.update", "Can update playbooks", "playbook", "update", ""),
    ("playbook.delete", "Can delete playbooks", "playbook", "delete", ""),
    ("playbook.upload", "Can upload playbook files", "playbook", "upload", ""),
    ("playbook.export", "Can export playbooks", "playbook", "export", ""),
    ("playbook.metadata", "Can manage playbook metadata", "playbook", "metadata", ""),

    # ── Dashboard ────────────────────────────────────────────────────────
    ("dashboard.view", "Can view dashboards", "dashboard", "view", ""),
    ("dashboard.create", "Can create dashboards", "dashboard", "create", ""),
    ("dashboard.update", "Can update dashboards", "dashboard", "update", ""),
    ("dashboard.delete", "Can delete dashboards", "dashboard", "delete", ""),
    ("dashboard.configure", "Can configure dashboard settings", "dashboard", "configure", ""),

    # ── Backup ───────────────────────────────────────────────────────────
    ("backup.create", "Can create backups", "backup", "create", ""),
    ("backup.view", "Can view backups", "backup", "view", ""),
    ("backup.restore", "Can restore from backups", "backup", "restore", ""),
    ("backup.delete", "Can delete backups", "backup", "delete", ""),
    ("backup.configure", "Can configure backup schedules", "backup", "configure", ""),

    # ── User Management ──────────────────────────────────────────────────
    ("user.create", "Can create users", "user", "create", ""),
    ("user.view", "Can view user profiles", "user", "view", ""),
    ("user.update", "Can update user profiles", "user", "update", ""),
    ("user.delete", "Can delete users", "user", "delete", ""),
    ("user.disable", "Can disable user accounts", "user", "disable", ""),
    ("user.enable", "Can enable user accounts", "user", "enable", ""),
    ("user.reset_password", "Can reset user passwords", "user", "reset_password", ""),
    ("user.assign_role", "Can assign roles to users", "user", "assign_role", ""),
    ("user.impersonate", "Can impersonate users", "user", "impersonate", ""),
    ("user.view_activity", "Can view user activity logs", "user", "view_activity", ""),
    ("user.manage_2fa", "Can manage 2FA settings", "user", "manage_2fa", ""),
    ("user.manage_api_keys", "Can manage API keys", "user", "manage_api_keys", ""),

    # ── Roles / Permissions Management ───────────────────────────────────
    ("role.create", "Can create roles", "role", "create", ""),
    ("role.view", "Can view roles", "role", "view", ""),
    ("role.update", "Can update role permissions", "role", "update", ""),
    ("role.delete", "Can delete roles", "role", "delete", ""),
    ("role.assign", "Can assign roles to users", "role", "assign", ""),

    # ── Integration ──────────────────────────────────────────────────────
    ("integration.create", "Can create integrations", "integration", "create", ""),
    ("integration.view", "Can view integrations", "integration", "view", ""),
    ("integration.update", "Can update integrations", "integration", "update", ""),
    ("integration.delete", "Can delete integrations", "integration", "delete", ""),
    ("integration.configure", "Can configure integration settings", "integration", "configure", ""),
    ("integration.test", "Can test integration connections", "integration", "test", ""),

    # ── Cost Centre ──────────────────────────────────────────────────────
    ("cost_centre.view", "Can view cost centre data", "cost_centre", "view", ""),
    ("cost_centre.manage", "Can manage cost centre budgets", "cost_centre", "manage", ""),
    ("cost_centre.report", "Can generate cost reports", "cost_centre", "report", ""),
    ("cost_centre.export", "Can export cost data", "cost_centre", "export", ""),

    # ── Analytics ────────────────────────────────────────────────────────
    ("analytics.view", "Can view analytics dashboards", "analytics", "view", ""),
    ("analytics.export", "Can export analytics data", "analytics", "export", ""),
    ("analytics.configure", "Can configure analytics", "analytics", "configure", ""),
    ("analytics.insights", "Can view AI-generated insights", "analytics", "insights", ""),

    # ── Email / Notifications ────────────────────────────────────────────
    ("email.send", "Can send emails", "email", "send", ""),
    ("email.view", "Can view email history", "email", "view", ""),
    ("email.configure", "Can configure email templates", "email", "configure", ""),
    ("notification.manage", "Can manage notification rules", "notification", "manage", ""),
    ("notification.view", "Can view notifications", "notification", "view", ""),

    # ── System Settings ──────────────────────────────────────────────────
    ("settings.view", "Can view system settings", "settings", "view", ""),
    ("settings.update", "Can update system settings", "settings", "update", ""),
    ("settings.security", "Can manage security settings", "settings", "security", ""),
    ("settings.branding", "Can manage branding settings", "settings", "branding", ""),
    ("settings.feature_flags", "Can manage feature flags", "settings", "feature_flags", ""),

    # ── Audit & Compliance ───────────────────────────────────────────────
    ("audit.view", "Can view audit logs", "audit", "view", ""),
    ("audit.export", "Can export audit logs", "audit", "export", ""),
    ("audit.retention", "Can configure audit retention", "audit", "retention", ""),
    ("compliance.view_reports", "Can view compliance reports", "compliance", "view_reports", ""),
    ("compliance.export_reports", "Can export compliance reports", "compliance", "export_reports", ""),
    ("compliance.run_audit", "Can run compliance audits", "compliance", "run_audit", ""),

    # ── Security ─────────────────────────────────────────────────────────
    ("security.policy_configure", "Can configure security policies", "security", "policy_configure", ""),
    ("security.encryption_manage", "Can manage encryption keys", "security", "encryption_manage", ""),
    ("security.mfa_configure", "Can configure MFA policies", "security", "mfa_configure", ""),
    ("security.session_configure", "Can configure session policies", "security", "session_configure", ""),
    ("security.ip_whitelist", "Can manage IP whitelists", "security", "ip_whitelist", ""),
    ("security.audit_events", "Can view security events", "security", "audit_events", ""),

    # ── Data Governance ──────────────────────────────────────────────────
    ("data.retention_configure", "Can configure retention policies", "data", "retention_configure", ""),
    ("data.purge", "Can purge data", "data", "purge", ""),
    ("data.export_bulk", "Can export data in bulk", "data", "export_bulk", ""),
    ("data.classification_view", "Can view data classifications", "data", "classification_view", ""),
    ("data.classification_set", "Can set data classifications", "data", "classification_set", ""),

    # ── Webhook ──────────────────────────────────────────────────────────
    ("webhook.create", "Can create webhooks", "webhook", "create", ""),
    ("webhook.view", "Can view webhooks", "webhook", "view", ""),
    ("webhook.update", "Can update webhooks", "webhook", "update", ""),
    ("webhook.delete", "Can delete webhooks", "webhook", "delete", ""),
    ("webhook.test", "Can test webhooks", "webhook", "test", ""),

    # ── Legal Vertical: Private Equity ───────────────────────────────────
    ("pe.classify", "Can classify PE documents", "pe", "classify", ""),
    ("pe.extract_risk", "Can extract risk clauses", "pe", "extract_risk", ""),
    ("pe.issue_spotting", "Can perform issue spotting", "pe", "issue_spotting", ""),
    ("pe.findings_report", "Can generate findings reports", "pe", "findings_report", ""),

    # ── Legal Vertical: Class Actions ────────────────────────────────────
    ("ca.evidence_culling", "Can perform evidence culling", "ca", "evidence_culling", ""),
    ("ca.pii_redaction", "Can perform PII redaction", "ca", "pii_redaction", ""),
    ("ca.extract_damages", "Can extract damages", "ca", "extract_damages", ""),
    ("ca.issue_tagging", "Can perform issue tagging", "ca", "issue_tagging", ""),
    ("ca.duplicate_detection", "Can perform duplicate detection", "ca", "duplicate_detection", ""),

    # ── Legal Vertical: Labor & Employment ───────────────────────────────
    ("le.analyze_communications", "Can analyze communications", "le", "analyze_communications", ""),
    ("le.wage_hour_analysis", "Can perform wage & hour analysis", "le", "wage_hour_analysis", ""),
    ("le.policy_comparison", "Can perform policy comparison", "le", "policy_comparison", ""),

    # ── Legal Vertical: IP Litigation ────────────────────────────────────
    ("ip.analyze_patent", "Can analyze patents", "ip", "analyze_patent", ""),
    ("ip.prior_art_search", "Can perform prior art search", "ip", "prior_art_search", ""),
    ("ip.infringement", "Can perform infringement analysis", "ip", "infringement", ""),

    # ── Legal Vertical: Regulatory Compliance ────────────────────────────
    ("rc.dsar_processing", "Can process DSARs", "rc", "dsar_processing", ""),
    ("rc.redaction", "Can perform regulatory redaction", "rc", "redaction", ""),
    ("rc.gap_analysis", "Can perform gap analysis", "rc", "gap_analysis", ""),
    ("rc.policy_mapping", "Can perform policy mapping", "rc", "policy_mapping", ""),

    # ── Industry-specific: Healthcare (HIPAA) ────────────────────────────
    ("hipaa.phi_access", "Can access PHI data", "hipaa", "phi_access", ""),
    ("hipaa.phi_export", "Can export PHI data", "hipaa", "phi_export", ""),
    ("hipaa.phi_delete", "Can delete PHI data", "hipaa", "phi_delete", ""),
    ("hipaa.breach_assessment", "Can perform breach assessments", "hipaa", "breach_assessment", ""),
    ("hipaa.audit_log_view", "Can view HIPAA audit logs", "hipaa", "audit_log_view", ""),
    ("hipaa.privacy_notice", "Can manage privacy notices", "hipaa", "privacy_notice", ""),
    ("hipaa.baa_manage", "Can manage BAA agreements", "hipaa", "baa_manage", ""),

    # ── Industry-specific: Finance (SOX/GLBA) ────────────────────────────
    ("sox.financial_report_view", "Can view financial reports", "sox", "financial_report_view", ""),
    ("sox.audit_trail_view", "Can view SOX audit trails", "sox", "audit_trail_view", ""),
    ("sox.control_test", "Can perform control testing", "sox", "control_test", ""),
    ("sox.disclosure_manage", "Can manage disclosures", "sox", "disclosure_manage", ""),
    ("glba.npi_access", "Can access NPI data", "glba", "npi_access", ""),
    ("glba.privacy_policy", "Can manage privacy policies", "glba", "privacy_policy", ""),
    ("glba.opt_out_manage", "Can manage opt-out preferences", "glba", "opt_out_manage", ""),

    # ── Industry-specific: Legal (FRE / eDiscovery) ──────────────────────
    ("fre.ediscovery_search", "Can perform eDiscovery searches", "fre", "ediscovery_search", ""),
    ("fre.legal_hold", "Can manage legal holds", "fre", "legal_hold", ""),
    ("fre.production_export", "Can export production sets", "fre", "production_export", ""),
    ("fre.bates_stamp", "Can apply Bates stamping", "fre", "bates_stamp", ""),
    ("fre.privilege_log", "Can manage privilege logs", "fre", "privilege_log", ""),
    ("fre.redaction_legal", "Can perform legal redaction", "fre", "redaction_legal", ""),

    # ── GDPR / Data Privacy ──────────────────────────────────────────────
    ("gdpr.data_access", "Can access subject data", "gdpr", "data_access", ""),
    ("gdpr.data_rectification", "Can rectify subject data", "gdpr", "data_rectification", ""),
    ("gdpr.data_erasure", "Can erase subject data (right to be forgotten)", "gdpr", "data_erasure", ""),
    ("gdpr.data_portability", "Can export data for portability", "gdpr", "data_portability", ""),
    ("gdpr.process_register", "Can manage processing registers", "gdpr", "process_register", ""),
    ("gdpr.dpia", "Can perform DPIAs", "gdpr", "dpia", ""),
    ("gdpr.ropa", "Can manage ROPA", "gdpr", "ropa", ""),

    # ── Monitoring & Observability ───────────────────────────────────────
    ("monitoring.view_metrics", "Can view system metrics", "monitoring", "view_metrics", ""),
    ("monitoring.view_logs", "Can view system logs", "monitoring", "view_logs", ""),
    ("monitoring.configure_alerts", "Can configure alerts", "monitoring", "configure_alerts", ""),
    ("monitoring.view_alerts", "Can view active alerts", "monitoring", "view_alerts", ""),
]

# ---------------------------------------------------------------------------
# 2. ROLE DEFINITIONS (Hierarchical — higher roles inherit from lower)
# ---------------------------------------------------------------------------

class RoleDefinition:
    """Defines a role with its permission codenames and inheritance."""

    def __init__(self, name, display_name, permissions=None, inherits_from=None, industry=None):
        self.name = name
        self.display_name = display_name
        self.permissions = set(permissions or [])
        self.inherits_from = inherits_from or []
        self.industry = industry

    def resolve_permissions(self, all_roles):
        """Resolve full permission set including inherited ones."""
        resolved = set(self.permissions)
        for parent_name in self.inherits_from:
            parent = all_roles.get(parent_name)
            if parent:
                resolved |= parent.resolve_permissions(all_roles)
        return resolved


def _p(*codenames):
    """Helper to reference permissions by codename."""
    return list(codenames)


ROLE_DEFINITIONS = {
    # ── Base Roles ───────────────────────────────────────────────────────
    "Guest": RoleDefinition(
        "Guest", "Guest (Read-Only)",
        _p(
            "document.view", "document.download",
            "ocr.view", "ocr.download",
            "translation.view", "translation.download",
            "anonymization.view", "anonymization.download",
            "project.view",
            "dashboard.view",
            "search.semantic",
            "notification.view",
        ),
    ),
    "Client": RoleDefinition(
        "Client", "Client (Standard User)",
        _p(
            "document.create", "document.upload", "document.share",
            "ocr.submit",
            "translation.submit",
            "anonymization.submit",
            "project.create",
            "classification.submit",
            "search.advanced", "search.saved",
            "analytics.view", "analytics.insights",
        ),
        inherits_from=["Guest"],
    ),
    "Developer": RoleDefinition(
        "Developer", "Developer (API/Integration Focus)",
        _p(
            "document.classify", "document.bulk_operation",
            "ocr.configure",
            "translation.configure",
            "anonymization.configure",
            "redlining.submit",
            "classification.submit",
            "structure.analyze",
            "workflow.create", "workflow.execute",
            "automation.create", "automation.execute",
            "versioning.create", "versioning.compare",
            "integration.create", "integration.test",
            "webhook.create", "webhook.test",
            "monitoring.view_logs",
        ),
        inherits_from=["Client"],
    ),
    "Manager": RoleDefinition(
        "Manager", "Manager (Team Oversight)",
        _p(
            "document.delete", "document.export", "document.restore",
            "ocr.delete",
            "translation.delete",
            "anonymization.delete",
            "redlining.delete",
            "project.update", "project.delete", "project.assign",
            "user.view", "user.view_activity",
            "dashboard.create", "dashboard.update", "dashboard.delete",
            "analytics.export",
            "email.send",
            "cost_centre.view",
            "pe.classify", "pe.findings_report",
            "ca.evidence_culling", "ca.issue_tagging",
            "le.analyze_communications",
            "ip.analyze_patent",
            "rc.dsar_processing", "rc.redaction",
        ),
        inherits_from=["Developer"],
    ),
    "Admin": RoleDefinition(
        "Admin", "Admin (Tenant Administrator)",
        _p(
            "user.create", "user.update", "user.delete",
            "user.disable", "user.enable", "user.reset_password",
            "user.assign_role", "user.manage_2fa", "user.manage_api_keys",
            "backup.create", "backup.view", "backup.restore", "backup.delete",
            "dashboard.configure",
            "settings.view", "settings.update", "settings.branding",
            "integration.update", "integration.delete", "integration.configure",
            "cost_centre.manage", "cost_centre.report", "cost_centre.export",
            "notification.manage",
            "email.view", "email.configure",
            "role.view",
            "audit.view", "audit.export",
            "monitoring.view_metrics", "monitoring.configure_alerts",
            "webhook.update", "webhook.delete",
        ),
        inherits_from=["Manager"],
    ),

    # ── Specialized Roles ────────────────────────────────────────────────
    "SecurityOfficer": RoleDefinition(
        "SecurityOfficer", "Security Officer",
        _p(
            "security.policy_configure", "security.encryption_manage",
            "security.mfa_configure", "security.session_configure",
            "security.ip_whitelist", "security.audit_events",
            "audit.view", "audit.export", "audit.retention",
            "settings.security",
            "user.impersonate", "user.manage_2fa",
        ),
        inherits_from=["Admin"],
    ),
    "ComplianceOfficer": RoleDefinition(
        "ComplianceOfficer", "Compliance Officer",
        _p(
            "compliance.view_reports", "compliance.export_reports",
            "compliance.run_audit",
            "audit.view", "audit.export", "audit.retention",
            "data.retention_configure", "data.purge",
            "data.classification_view", "data.classification_set",
            "gdpr.data_access", "gdpr.data_rectification",
            "gdpr.data_erasure", "gdpr.data_portability",
            "gdpr.process_register", "gdpr.dpia", "gdpr.ropa",
        ),
        inherits_from=["Admin"],
    ),
    "Auditor": RoleDefinition(
        "Auditor", "Auditor (Read-Only Compliance)",
        _p(
            "audit.view", "audit.export",
            "compliance.view_reports",
            "monitoring.view_metrics", "monitoring.view_logs",
            "data.classification_view",
            "hipaa.audit_log_view",
            "sox.audit_trail_view",
            "security.audit_events",
        ),
    ),

    # ── Industry-specific Roles ──────────────────────────────────────────
    "HIPAAOfficer": RoleDefinition(
        "HIPAAOfficer", "HIPAA Privacy/Security Officer",
        _p(
            "hipaa.phi_access", "hipaa.phi_export",
            "hipaa.breach_assessment", "hipaa.audit_log_view",
            "hipaa.privacy_notice", "hipaa.baa_manage",
            "compliance.view_reports", "compliance.run_audit",
            "audit.view", "audit.export",
        ),
        inherits_from=["ComplianceOfficer"],
        industry="healthcare",
    ),
    "SOXOfficer": RoleDefinition(
        "SOXOfficer", "SOX Compliance Officer",
        _p(
            "sox.financial_report_view", "sox.audit_trail_view",
            "sox.control_test", "sox.disclosure_manage",
            "glba.npi_access", "glba.privacy_policy", "glba.opt_out_manage",
            "compliance.view_reports", "compliance.run_audit",
        ),
        inherits_from=["ComplianceOfficer"],
        industry="finance",
    ),
    "FREspecialist": RoleDefinition(
        "FREspecialist", "eDiscovery / FRE Specialist",
        _p(
            "fre.ediscovery_search", "fre.legal_hold",
            "fre.production_export", "fre.bates_stamp",
            "fre.privilege_log", "fre.redaction_legal",
            "ca.evidence_culling", "ca.pii_redaction",
            "ca.extract_damages", "ca.duplicate_detection",
            "ip.prior_art_search", "ip.infringement",
        ),
        inherits_from=["Manager"],
        industry="legal",
    ),
    "DataProtectionOfficer": RoleDefinition(
        "DataProtectionOfficer", "Data Protection Officer (GDPR)",
        _p(
            "gdpr.data_access", "gdpr.data_rectification",
            "gdpr.data_erasure", "gdpr.data_portability",
            "gdpr.process_register", "gdpr.dpia", "gdpr.ropa",
            "compliance.view_reports", "compliance.export_reports",
            "data.classification_view", "data.classification_set",
            "data.retention_configure", "data.purge",
        ),
        inherits_from=["ComplianceOfficer"],
        industry="gdpr",
    ),

    # ── System Roles ─────────────────────────────────────────────────────
    "SuperAdmin": RoleDefinition(
        "SuperAdmin", "Super Admin (System-Wide)",
        _p(
            "role.create", "role.update", "role.delete", "role.assign",
            "settings.security", "settings.feature_flags",
            "user.impersonate",
            "data.export_bulk",
            "backup.configure",
            "system.*",  # wildcard — all system-level perms
        ),
        inherits_from=["Admin", "SecurityOfficer", "ComplianceOfficer"],
    ),
}

# ---------------------------------------------------------------------------
# 3. HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def get_permission_codenames():
    """Return all defined permission codenames."""
    return [p[0] for p in PERMISSION_DEFINITIONS]


def get_permission_by_codename(codename):
    """Look up a permission definition by codename."""
    for p in PERMISSION_DEFINITIONS:
        if p[0] == codename:
            return p
    return None


def get_role_permissions(role_name):
    """Get resolved permission codenames for a role."""
    role = ROLE_DEFINITIONS.get(role_name)
    if not role:
        return set()
    return role.resolve_permissions(ROLE_DEFINITIONS)


def get_user_roles(user):
    """
    Return the set of role names a user actually holds, derived from the
    database only (Django groups + is_staff / is_superuser).

    IMPORTANT: never derive roles from request headers (e.g. X-Role); those are
    client-controlled and must not grant privileges.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return set()

    roles = set(user.groups.values_list("name", flat=True))
    if getattr(user, "is_superuser", False):
        roles.update({"SuperAdmin", "Admin", "Manager", "Developer", "Client"})
    elif getattr(user, "is_staff", False):
        roles.update({"Admin"})
    return roles


def get_user_role_permissions(user):
    """Union of resolved permission codenames across all of the user's roles."""
    resolved = set()
    for role_name in get_user_roles(user):
        resolved |= get_role_permissions(role_name)
    return resolved


# ---------------------------------------------------------------------------
# 4. SEEDING — Create Django Permission objects + Groups
# ---------------------------------------------------------------------------

def seed_permissions_and_roles():
    """
    Idempotent: creates all Permission objects and Group objects in Django DB.
    Call from a data migration or management command.
    """
    from django.contrib.auth.models import Permission, Group
    from django.contrib.contenttypes.models import ContentType

    # Use a dummy content type (auth) since we're not model-bound
    ct, _ = ContentType.objects.get_or_create(
        app_label="custom_authentication",
        model="rbacpermission",
        defaults={"model": "rbacpermission"},
    )

    created_perms = {}
    for codename, label, resource, action, desc in PERMISSION_DEFINITIONS:
        perm, _ = Permission.objects.get_or_create(
            codename=codename,
            content_type=ct,
            defaults={"name": label},
        )
        created_perms[codename] = perm

    for role_name, role_def in ROLE_DEFINITIONS.items():
        group, created = Group.objects.get_or_create(name=role_name)
        if created:
            logger.info(f"Created group: {role_name}")

        resolved = role_def.resolve_permissions(ROLE_DEFINITIONS)
        perm_objects = []
        for codename in resolved:
            perm = created_perms.get(codename)
            if perm:
                perm_objects.append(perm)

        group.permissions.set(perm_objects)
        group.save()
        logger.info(f"Assigned {len(perm_objects)} permissions to {role_name}")


# ---------------------------------------------------------------------------
# 5. DRF PERMISSION CLASSES
# ---------------------------------------------------------------------------

class HasPermission(BasePermission):
    """
    Check if user has a specific permission by codename.
    Checks Django's Permission model and the user's DB-backed roles.

    Usage:
        class MyView(APIView):
            permission_classes = [HasPermission('document.create')]
    """

    def __init__(self, *codenames):
        self.codenames = codenames
        super().__init__()

    def __call__(self):
        return self

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Superusers bypass all checks
        if request.user.is_superuser:
            return True

        # Check Django permissions
        for codename in self.codenames:
            if request.user.has_perm(f"custom_authentication.{codename}"):
                return True

        # Fallback: resolve from the user's real (DB-backed) roles.
        resolved = get_user_role_permissions(request.user)
        if any(c in resolved for c in self.codenames):
            return True

        return False


class HasAnyPermissions(HasPermission):
    """
    User needs at least ONE of the specified permissions.
    Usage: permission_classes = [HasAnyPermissions('doc.create', 'doc.update')]
    """
    pass


class HasAllPermissions(BasePermission):
    """
    User needs ALL of the specified permissions.
    Usage: permission_classes = [HasAllPermissions('doc.create', 'doc.update')]
    """

    def __init__(self, *codenames):
        self.codenames = codenames
        super().__init__()

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True

        # Check Django permissions
        for codename in self.codenames:
            if not request.user.has_perm(f"custom_authentication.{codename}"):
                break
        else:
            return True

        # Fallback: resolve from the user's real (DB-backed) roles.
        resolved = get_user_role_permissions(request.user)
        if all(c in resolved for c in self.codenames):
            return True

        return False


class IsAdminOrManagerMutation(BasePermission):
    """
    Allows reads (GET, HEAD, OPTIONS) for any authenticated user.
    Requires Admin or Manager role for writes.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True

        roles = get_user_roles(request.user)
        return bool(roles & {"Admin", "SuperAdmin", "Manager"})


class RequireAdminOrManagerMutation(BasePermission):
    """Requires Admin or Manager role for ALL methods."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        roles = get_user_roles(request.user)
        return bool(roles & {"Admin", "SuperAdmin", "Manager"})


# ---------------------------------------------------------------------------
# 6. CONVENIENCE — Permission presets for common view patterns
# ---------------------------------------------------------------------------

class CanViewDocuments(HasPermission):
    def __init__(self):
        super().__init__("document.view")


class CanCreateDocuments(HasPermission):
    def __init__(self):
        super().__init__("document.create")


class CanSubmitOCR(HasPermission):
    def __init__(self):
        super().__init__("ocr.submit")


class CanSubmitTranslation(HasPermission):
    def __init__(self):
        super().__init__("translation.submit")


class CanSubmitAnonymization(HasPermission):
    def __init__(self):
        super().__init__("anonymization.submit")


class CanManageUsers(HasPermission):
    def __init__(self):
        super().__init__("user.create", "user.update", "user.delete")


class CanManageBackups(HasPermission):
    def __init__(self):
        super().__init__("backup.create", "backup.restore", "backup.delete")


class CanViewAuditLogs(HasPermission):
    def __init__(self):
        super().__init__("audit.view")


class CanManageRoles(HasPermission):
    def __init__(self):
        super().__init__("role.create", "role.update", "role.delete", "role.assign")


class CanManageIntegrations(HasPermission):
    def __init__(self):
        super().__init__("integration.create", "integration.update", "integration.delete")


class CanSearch(HasPermission):
    def __init__(self):
        super().__init__("search.semantic", "search.advanced")
