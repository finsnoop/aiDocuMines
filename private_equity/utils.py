"""
Utility functions for the Private Equity Due Diligence application.
"""
import re
import logging
from typing import Dict, List, Optional, Tuple
from django.contrib.auth import get_user_model
from core.models import File
from .models import DocumentClassification, RiskClause, DueDiligenceRun

logger = logging.getLogger(__name__)
User = get_user_model()


def extract_document_text(file_path: str) -> str:
    """
    Extract text content from various document formats.
    This is a placeholder implementation - replace with actual text extraction logic.
    """
    try:
        # Placeholder text extraction
        # In a real implementation, this would use libraries like:
        # - pypdf or pdfplumber for PDFs
        # - python-docx for Word documents
        # - openpyxl for Excel files
        # - etc.
        
        if file_path.lower().endswith('.pdf'):
            return "Sample PDF text content for due diligence analysis..."
        elif file_path.lower().endswith(('.doc', '.docx')):
            return "Sample Word document text content for contract analysis..."
        elif file_path.lower().endswith(('.xls', '.xlsx')):
            return "Sample Excel spreadsheet content for financial analysis..."
        else:
            return "Sample text content for document analysis..."
            
    except Exception as e:
        logger.error(f"Failed to extract text from {file_path}: {str(e)}")
        return ""


def classify_document_by_content(text_content: str) -> Tuple[str, float]:
    """
    Classify document type based on text content using keyword matching.
    Returns tuple of (document_type, confidence_score).
    This is a placeholder implementation - replace with actual ML classification.
    """
    text_lower = text_content.lower()
    
    # Define keyword patterns for different document types
    patterns = {
        'nda': ['non-disclosure', 'confidential', 'proprietary information', 'trade secret'],
        'employment_agreement': ['employment', 'employee', 'salary', 'benefits', 'termination'],
        'supplier_contract': ['supplier', 'vendor', 'purchase', 'delivery', 'goods'],
        'lease_agreement': ['lease', 'rent', 'premises', 'landlord', 'tenant'],
        'ip_document': ['patent', 'trademark', 'copyright', 'intellectual property'],
        'privacy_policy': ['privacy', 'personal data', 'cookies', 'gdpr', 'data protection'],
        'financial_statement': ['balance sheet', 'income statement', 'cash flow', 'revenue'],
        'audit_report': ['audit', 'auditor', 'opinion', 'material weakness', 'internal control'],
        'insurance_policy': ['insurance', 'policy', 'coverage', 'premium', 'claim'],
        'regulatory_filing': ['sec filing', 'regulatory', 'compliance', '10-k', '10-q']
    }
    
    best_match = 'other'
    best_score = 0.0
    
    for doc_type, keywords in patterns.items():
        matches = sum(1 for keyword in keywords if keyword in text_lower)
        score = matches / len(keywords)
        
        if score > best_score:
            best_score = score
            best_match = doc_type
    
    # Adjust confidence based on match quality
    if best_score > 0.5:
        confidence = min(0.95, 0.6 + best_score * 0.35)
    elif best_score > 0.2:
        confidence = 0.4 + best_score * 0.4
    else:
        confidence = 0.3
    
    return best_match, confidence


def extract_risk_clauses_from_text(text_content: str, document_type: str) -> List[Dict]:
    """
    Extract risk clauses from document text using pattern matching.
    This is a placeholder implementation - replace with actual NLP extraction.
    """
    risk_clauses = []
    
    # Define risk patterns for different clause types
    risk_patterns = {
        'change_of_control': [
            r'change of control',
            r'acquisition.*termination',
            r'merger.*terminate',
            r'control.*change.*trigger'
        ],
        'assignment': [
            r'may not.*assign',
            r'assignment.*consent',
            r'transfer.*prohibited',
            r'assign.*without.*approval'
        ],
        'termination': [
            r'terminate.*immediately',
            r'termination.*cause',
            r'end.*agreement.*upon',
            r'cancel.*contract.*if'
        ],
        'indemnity': [
            r'indemnify.*against',
            r'hold.*harmless',
            r'defend.*claims',
            r'liability.*damages'
        ],
        'non_compete': [
            r'non.*compete',
            r'restraint.*trade',
            r'solicit.*customers',
            r'compete.*business'
        ],
        'data_privacy': [
            r'personal.*data',
            r'privacy.*breach',
            r'data.*protection',
            r'confidential.*information'
        ]
    }
    
    text_lower = text_content.lower()
    
    for clause_type, patterns in risk_patterns.items():
        for pattern in patterns:
            matches = re.finditer(pattern, text_lower)
            for match in matches:
                # Extract surrounding context
                start = max(0, match.start() - 100)
                end = min(len(text_content), match.end() + 100)
                clause_text = text_content[start:end].strip()
                
                # Determine risk level based on keywords
                risk_level = determine_risk_level(clause_text, clause_type)
                
                risk_clauses.append({
                    'clause_type': clause_type,
                    'clause_text': clause_text,
                    'risk_level': risk_level,
                    'position_start': match.start(),
                    'position_end': match.end(),
                    'risk_explanation': generate_risk_explanation(clause_type, risk_level),
                    'mitigation_suggestions': generate_mitigation_suggestions(clause_type, risk_level)
                })
    
    return risk_clauses


def determine_risk_level(clause_text: str, clause_type: str) -> str:
    """
    Determine risk level based on clause content and type.
    """
    text_lower = clause_text.lower()
    
    # High-risk indicators
    high_risk_keywords = [
        'immediately', 'without notice', 'sole discretion', 'unlimited liability',
        'personal guarantee', 'criminal', 'material breach'
    ]
    
    # Critical risk indicators
    critical_risk_keywords = [
        'terminate immediately', 'criminal liability', 'unlimited damages',
        'personal assets', 'joint and several'
    ]
    
    # Medium risk indicators
    medium_risk_keywords = [
        'reasonable notice', 'material change', 'written consent',
        'business days', 'cure period'
    ]
    
    if any(keyword in text_lower for keyword in critical_risk_keywords):
        return 'critical'
    elif any(keyword in text_lower for keyword in high_risk_keywords):
        return 'high'
    elif any(keyword in text_lower for keyword in medium_risk_keywords):
        return 'medium'
    else:
        return 'low'


def generate_risk_explanation(clause_type: str, risk_level: str) -> str:
    """
    Generate explanation for why a clause is considered risky.
    """
    explanations = {
        'change_of_control': {
            'critical': 'Change of control provisions could immediately terminate critical contracts upon deal closing.',
            'high': 'Change of control clauses may trigger contract termination or renegotiation.',
            'medium': 'Change of control provisions require notification but may not terminate contract.',
            'low': 'Change of control provisions are standard and unlikely to impact transaction.'
        },
        'assignment': {
            'critical': 'Assignment restrictions could prevent deal completion without consent.',
            'high': 'Assignment clauses may require third-party consent that could be withheld.',
            'medium': 'Assignment requires consent but is typically granted for legitimate transactions.',
            'low': 'Assignment provisions are standard and unlikely to create issues.'
        },
        'termination': {
            'critical': 'Termination clauses could result in immediate loss of critical contracts.',
            'high': 'Termination provisions may be triggered by transaction activities.',
            'medium': 'Termination clauses require careful management during transaction.',
            'low': 'Termination provisions are standard commercial terms.'
        }
    }
    
    return explanations.get(clause_type, {}).get(risk_level, 'Risk level requires further analysis.')


def generate_mitigation_suggestions(clause_type: str, risk_level: str) -> str:
    """
    Generate mitigation suggestions for risk clauses.
    """
    suggestions = {
        'change_of_control': {
            'critical': 'Negotiate carve-out for planned transactions or obtain advance consent.',
            'high': 'Seek amendment to exclude planned transaction from change of control definition.',
            'medium': 'Provide required notifications and monitor for any objections.',
            'low': 'Standard compliance with notification requirements should suffice.'
        },
        'assignment': {
            'critical': 'Obtain written consent from counterparty prior to closing.',
            'high': 'Initiate consent process early and have backup plans if consent denied.',
            'medium': 'Request consent as part of standard transaction process.',
            'low': 'Include in routine consent requests to counterparties.'
        },
        'termination': {
            'critical': 'Negotiate amendment to exclude transaction-related activities from termination triggers.',
            'high': 'Carefully structure transaction to avoid triggering termination clauses.',
            'medium': 'Monitor compliance with contract terms throughout transaction process.',
            'low': 'Maintain standard contract compliance practices.'
        }
    }
    
    return suggestions.get(clause_type, {}).get(risk_level, 'Consult legal counsel for specific mitigation strategies.')


def calculate_deal_risk_score(dd_run: DueDiligenceRun) -> Dict:
    """
    Calculate overall risk score for a due diligence run.
    """
    risk_clauses = RiskClause.objects.filter(due_diligence_run=dd_run)
    
    if not risk_clauses.exists():
        return {
            'overall_score': 0,
            'risk_level': 'low',
            'total_clauses': 0,
            'breakdown': {}
        }
    
    # Weight different risk levels
    risk_weights = {
        'critical': 10,
        'high': 5,
        'medium': 2,
        'low': 1
    }
    
    total_score = 0
    breakdown = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
    
    for clause in risk_clauses:
        weight = risk_weights.get(clause.risk_level, 1)
        total_score += weight
        breakdown[clause.risk_level] += 1
    
    # Normalize score (0-100)
    max_possible_score = risk_clauses.count() * 10
    normalized_score = min(100, (total_score / max_possible_score) * 100) if max_possible_score > 0 else 0
    
    # Determine overall risk level
    if normalized_score >= 70:
        overall_risk = 'critical'
    elif normalized_score >= 50:
        overall_risk = 'high'
    elif normalized_score >= 25:
        overall_risk = 'medium'
    else:
        overall_risk = 'low'
    
    return {
        'overall_score': round(normalized_score, 2),
        'risk_level': overall_risk,
        'total_clauses': risk_clauses.count(),
        'breakdown': breakdown
    }


def generate_executive_summary(dd_run: DueDiligenceRun) -> str:
    """
    Generate executive summary for a due diligence run.
    """
    risk_score_data = calculate_deal_risk_score(dd_run)
    doc_count = DocumentClassification.objects.filter(due_diligence_run=dd_run).count()
    
    summary = f"""
    Executive Summary - {dd_run.deal_name}
    
    Target Company: {dd_run.target_company}
    Deal Type: {dd_run.get_deal_type_display()}
    Documents Reviewed: {doc_count}
    
    Risk Assessment:
    - Overall Risk Level: {risk_score_data['risk_level'].title()}
    - Risk Score: {risk_score_data['overall_score']}/100
    - Total Risk Clauses: {risk_score_data['total_clauses']}
    
    Key Risk Areas:
    - Critical Risk Items: {risk_score_data['breakdown']['critical']}
    - High Risk Items: {risk_score_data['breakdown']['high']}
    - Medium Risk Items: {risk_score_data['breakdown']['medium']}
    
    This due diligence review identified {risk_score_data['total_clauses']} risk clauses 
    across {doc_count} documents. The overall risk level is assessed as 
    {risk_score_data['risk_level']} based on the nature and severity of identified issues.
    """
    
    return summary.strip()
