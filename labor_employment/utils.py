"""
Utility functions for the Labor Employment application.
"""
import re
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q
from django.db import models
from .models import (
    WorkplaceCommunicationsRun, CommunicationMessage, WageHourAnalysis,
    ComplianceAlert
)

logger = logging.getLogger(__name__)
User = get_user_model()


def extract_email_metadata(email_content: str) -> Dict:
    """
    Extract metadata from email content (headers, participants, etc.).
    """
    metadata = {
        'sender': '',
        'recipients': [],
        'subject': '',
        'sent_datetime': None,
        'message_id': ''
    }
    
    lines = email_content.split('\n')
    
    for line in lines:
        line = line.strip()
        
        # Extract sender
        if line.startswith('From:'):
            sender_match = re.search(r'[\w\.-]+@[\w\.-]+', line)
            if sender_match:
                metadata['sender'] = sender_match.group()
        
        # Extract recipients
        elif line.startswith('To:'):
            recipients = re.findall(r'[\w\.-]+@[\w\.-]+', line)
            metadata['recipients'] = recipients
        
        # Extract subject
        elif line.startswith('Subject:'):
            metadata['subject'] = line.replace('Subject:', '').strip()
        
        # Extract date
        elif line.startswith('Date:'):
            date_str = line.replace('Date:', '').strip()
            try:
                # Parse common email date formats
                metadata['sent_datetime'] = datetime.strptime(date_str, '%a, %d %b %Y %H:%M:%S %z')
            except ValueError:
                pass
        
        # Extract message ID
        elif line.startswith('Message-ID:'):
            metadata['message_id'] = line.replace('Message-ID:', '').strip().strip('<>')
    
    return metadata


def analyze_message_sentiment(content: str) -> float:
    """
    Analyze sentiment of message content.
    Returns score between -1.0 (negative) and 1.0 (positive).
    """
    # Simple keyword-based sentiment analysis
    positive_words = [
        'good', 'great', 'excellent', 'amazing', 'wonderful', 'fantastic',
        'happy', 'pleased', 'satisfied', 'thank', 'appreciate', 'love'
    ]
    
    negative_words = [
        'bad', 'terrible', 'awful', 'horrible', 'hate', 'angry', 'frustrated',
        'disappointed', 'upset', 'annoyed', 'concerned', 'worried', 'problem'
    ]
    
    content_lower = content.lower()
    
    positive_count = sum(1 for word in positive_words if word in content_lower)
    negative_count = sum(1 for word in negative_words if word in content_lower)
    
    total_words = len(content.split())
    
    if total_words == 0:
        return 0.0
    
    # Calculate sentiment score
    sentiment_score = (positive_count - negative_count) / max(1, total_words / 10)
    
    # Normalize to -1.0 to 1.0 range
    return max(-1.0, min(1.0, sentiment_score))


def analyze_message_toxicity(content: str) -> float:
    """
    Analyze toxicity level of message content.
    Returns score between 0.0 (not toxic) and 1.0 (highly toxic).
    """
    # Keywords that indicate potentially toxic content
    toxic_indicators = [
        'stupid', 'idiot', 'moron', 'incompetent', 'useless', 'pathetic',
        'disgusting', 'ridiculous', 'absurd', 'inappropriate', 'unacceptable',
        'harassment', 'discriminat', 'offensive', 'hostile'
    ]
    
    content_lower = content.lower()
    
    toxic_count = sum(1 for indicator in toxic_indicators if indicator in content_lower)
    total_words = len(content.split())
    
    if total_words == 0:
        return 0.0
    
    # Calculate toxicity score
    toxicity_score = toxic_count / max(1, total_words / 20)
    
    # Normalize to 0.0 to 1.0 range
    return min(1.0, toxicity_score)


def calculate_message_relevance(content: str, case_keywords: List[str]) -> float:
    """
    Calculate relevance of message to the case based on keywords.
    """
    if not case_keywords:
        return 0.5  # Default relevance if no keywords provided
    
    content_lower = content.lower()
    
    keyword_matches = sum(1 for keyword in case_keywords if keyword.lower() in content_lower)
    
    # Base relevance score
    relevance_score = keyword_matches / len(case_keywords)
    
    # Boost score for employment-related terms
    employment_terms = [
        'employee', 'employer', 'workplace', 'job', 'work', 'salary', 'wage',
        'overtime', 'harassment', 'discrimination', 'termination', 'firing',
        'promotion', 'demotion', 'performance', 'review', 'policy'
    ]
    
    employment_matches = sum(1 for term in employment_terms if term in content_lower)
    employment_boost = min(0.3, employment_matches * 0.05)
    
    return min(1.0, relevance_score + employment_boost)


def detect_overtime_indicators(content: str) -> List[str]:
    """
    Detect indicators of overtime work in message content.
    """
    indicators = []
    content_lower = content.lower()
    
    overtime_patterns = [
        r'work(?:ing)?\s+(?:late|overtime|extra\s+hours)',
        r'stay(?:ing)?\s+(?:late|after\s+hours)',
        r'weekend\s+work',
        r'work(?:ing)?\s+(?:saturday|sunday)',
        r'(?:before|after)\s+(?:7|8|9)\s*(?:am|pm)',
        r'(?:12|13|14|15|16|17|18|19|20)\s*hour\s+days?'
    ]
    
    for pattern in overtime_patterns:
        if re.search(pattern, content_lower):
            indicators.append(f"Overtime pattern: {pattern}")
    
    # Time-based indicators
    time_patterns = [
        r'(?:1[0-2]|[1-9]):[0-5][0-9]\s*(?:pm|am)',
        r'(?:2[0-3]|1[0-9]):[0-5][0-9]'  # 24-hour format
    ]
    
    for pattern in time_patterns:
        matches = re.findall(pattern, content_lower)
        for match in matches:
            # Check if it's outside normal business hours
            try:
                time_str = match.replace('pm', '').replace('am', '').strip()
                hour = int(time_str.split(':')[0])
                if 'pm' in match and hour < 12:
                    hour += 12
                elif 'am' in match and hour == 12:
                    hour = 0
                
                if hour < 7 or hour > 19:  # Before 7 AM or after 7 PM
                    indicators.append(f"Off-hours time reference: {match}")
            except (ValueError, IndexError):
                pass
    
    return indicators


def analyze_communication_patterns(comm_run: WorkplaceCommunicationsRun) -> Dict:
    """
    Analyze communication patterns for potential employment law issues.
    """
    messages = CommunicationMessage.objects.filter(communications_run=comm_run)
    
    if not messages.exists():
        return {"error": "No messages found for analysis"}
    
    # Analyze sender patterns
    sender_stats = {}
    for sender in messages.values_list('sender', flat=True).distinct():
        sender_messages = messages.filter(sender=sender)
        
        sender_stats[sender] = {
            'total_messages': sender_messages.count(),
            'avg_sentiment': sender_messages.aggregate(Avg('sentiment_score'))['sentiment_score__avg'] or 0,
            'avg_toxicity': sender_messages.aggregate(Avg('toxicity_score'))['toxicity_score__avg'] or 0,
            'flagged_messages': sender_messages.filter(is_flagged=True).count(),
            'off_hours_messages': sender_messages.filter(
                Q(sent_datetime__hour__lt=7) | Q(sent_datetime__hour__gt=19)
            ).count(),
            'weekend_messages': sender_messages.filter(
                sent_datetime__week_day__in=[1, 7]  # Sunday=1, Saturday=7
            ).count()
        }
    
    # Identify potential issues
    potential_issues = []
    
    for sender, stats in sender_stats.items():
        if stats['avg_toxicity'] > 0.5:
            potential_issues.append({
                'type': 'high_toxicity',
                'sender': sender,
                'description': f"High average toxicity score: {stats['avg_toxicity']:.2f}"
            })
        
        if stats['off_hours_messages'] > 10:
            potential_issues.append({
                'type': 'excessive_off_hours',
                'sender': sender,
                'description': f"Excessive off-hours communications: {stats['off_hours_messages']} messages"
            })
        
        if stats['avg_sentiment'] < -0.5:
            potential_issues.append({
                'type': 'negative_sentiment',
                'sender': sender,
                'description': f"Consistently negative sentiment: {stats['avg_sentiment']:.2f}"
            })
    
    return {
        'sender_statistics': sender_stats,
        'potential_issues': potential_issues,
        'total_messages': messages.count(),
        'analysis_period': {
            'start': comm_run.analysis_start_date.isoformat(),
            'end': comm_run.analysis_end_date.isoformat()
        }
    }


def generate_wage_hour_report(comm_run: WorkplaceCommunicationsRun) -> Dict:
    """
    Generate comprehensive wage and hour analysis report.
    """
    analyses = WageHourAnalysis.objects.filter(communications_run=comm_run)
    
    if not analyses.exists():
        return {"error": "No wage hour analyses found"}
    
    # Aggregate statistics
    total_employees = analyses.count()
    total_overtime_hours = analyses.aggregate(total_ot=models.Sum('overtime_hours'))['total_ot'] or 0
    total_potential_violations = analyses.filter(
        Q(potential_overtime_violations=True) |
        Q(potential_break_violations=True) |
        Q(potential_meal_violations=True)
    ).count()
    
    # Calculate potential unpaid overtime
    unpaid_overtime_amount = 0
    for analysis in analyses:
        if analysis.potential_overtime_violations and analysis.hourly_rate:
            unpaid_overtime_amount += analysis.overtime_hours * analysis.hourly_rate * 1.5
    
    # Identify high-risk employees
    high_risk_employees = []
    for analysis in analyses:
        risk_score = 0
        risk_factors = []
        
        if analysis.potential_overtime_violations:
            risk_score += 3
            risk_factors.append("Overtime violations")
        
        if analysis.early_morning_messages > 10:
            risk_score += 2
            risk_factors.append("Excessive early morning communications")
        
        if analysis.late_evening_messages > 15:
            risk_score += 2
            risk_factors.append("Excessive late evening communications")
        
        if analysis.weekend_messages > 5:
            risk_score += 1
            risk_factors.append("Weekend work communications")
        
        if risk_score >= 4:
            high_risk_employees.append({
                'employee_name': analysis.employee_name,
                'risk_score': risk_score,
                'risk_factors': risk_factors,
                'overtime_hours': float(analysis.overtime_hours),
                'potential_unpaid_amount': float(analysis.overtime_hours * (analysis.hourly_rate or 0) * 1.5)
            })
    
    return {
        'summary': {
            'total_employees_analyzed': total_employees,
            'total_overtime_hours': float(total_overtime_hours),
            'employees_with_violations': total_potential_violations,
            'estimated_unpaid_overtime': float(unpaid_overtime_amount),
            'violation_rate': (total_potential_violations / total_employees * 100) if total_employees > 0 else 0
        },
        'high_risk_employees': high_risk_employees,
        'recommendations': generate_wage_hour_recommendations(analyses)
    }


def generate_wage_hour_recommendations(analyses) -> List[str]:
    """
    Generate recommendations based on wage hour analysis.
    """
    recommendations = []
    
    overtime_violations = analyses.filter(potential_overtime_violations=True).count()
    total_analyses = analyses.count()
    
    if overtime_violations > 0:
        violation_rate = (overtime_violations / total_analyses) * 100
        recommendations.append(f"Address potential overtime violations affecting {overtime_violations} employees ({violation_rate:.1f}%)")
        
        if violation_rate > 25:
            recommendations.append("Consider implementing automated time tracking systems")
            recommendations.append("Review and update overtime policies and procedures")
    
    excessive_off_hours = analyses.filter(
        Q(early_morning_messages__gt=10) | Q(late_evening_messages__gt=15)
    ).count()
    
    if excessive_off_hours > 0:
        recommendations.append("Establish clear boundaries for after-hours communications")
        recommendations.append("Consider implementing 'right to disconnect' policies")
    
    weekend_workers = analyses.filter(weekend_messages__gt=5).count()
    if weekend_workers > 0:
        recommendations.append("Review weekend work practices and compensation")
    
    return recommendations


def validate_eeoc_packet_data(packet_data: Dict) -> List[str]:
    """
    Validate EEOC packet data for completeness and accuracy.
    """
    errors = []
    
    # Required fields
    required_fields = [
        'packet_name', 'complainant_name', 'complaint_type',
        'incident_date', 'complaint_summary'
    ]
    
    for field in required_fields:
        if not packet_data.get(field):
            errors.append(f"{field.replace('_', ' ').title()} is required")
    
    # Date validation
    incident_date = packet_data.get('incident_date')
    if incident_date:
        try:
            if isinstance(incident_date, str):
                datetime.fromisoformat(incident_date.replace('Z', '+00:00'))
        except ValueError:
            errors.append("Invalid incident date format")
    
    # Complaint summary length
    complaint_summary = packet_data.get('complaint_summary', '')
    if len(complaint_summary) < 50:
        errors.append("Complaint summary should be at least 50 characters")
    
    return errors


def export_communication_data(comm_run: WorkplaceCommunicationsRun, format_type: str = 'csv') -> str:
    """
    Export communication data in specified format.
    """
    messages = CommunicationMessage.objects.filter(communications_run=comm_run)
    
    if format_type.lower() == 'csv':
        import csv
        import io
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            'Message ID', 'Sender', 'Recipients', 'Subject', 'Sent DateTime',
            'Message Type', 'Sentiment Score', 'Toxicity Score', 'Relevance Score',
            'Is Flagged', 'Flag Reason', 'Contains PII', 'Is Privileged'
        ])
        
        # Write data
        for message in messages:
            writer.writerow([
                message.message_id,
                message.sender,
                ', '.join(message.recipients),
                message.subject,
                message.sent_datetime.strftime('%Y-%m-%d %H:%M:%S'),
                message.get_message_type_display(),
                message.sentiment_score,
                message.toxicity_score,
                message.relevance_score,
                message.is_flagged,
                message.flag_reason,
                message.contains_pii,
                message.is_privileged
            ])
        
        return output.getvalue()
    
    else:
        raise ValueError(f"Unsupported export format: {format_type}")


def calculate_case_risk_score(comm_run: WorkplaceCommunicationsRun) -> Dict:
    """
    Calculate overall risk score for an employment case.
    """
    # Get compliance alerts
    alerts = ComplianceAlert.objects.filter(communications_run=comm_run)
    
    # Calculate risk factors
    critical_alerts = alerts.filter(severity='critical').count()
    high_alerts = alerts.filter(severity='high').count()
    medium_alerts = alerts.filter(severity='medium').count()
    
    # Get message statistics
    messages = CommunicationMessage.objects.filter(communications_run=comm_run)
    total_messages = messages.count()
    
    if total_messages == 0:
        return {'overall_risk': 'low', 'risk_score': 0, 'risk_factors': []}
    
    flagged_messages = messages.filter(is_flagged=True).count()
    high_toxicity_messages = messages.filter(toxicity_score__gte=0.7).count()
    negative_sentiment_messages = messages.filter(sentiment_score__lte=-0.5).count()
    
    # Calculate weighted risk score
    risk_score = (
        critical_alerts * 10 +
        high_alerts * 5 +
        medium_alerts * 2 +
        (flagged_messages / max(1, total_messages)) * 20 +
        (high_toxicity_messages / max(1, total_messages)) * 15 +
        (negative_sentiment_messages / max(1, total_messages)) * 10
    )
    
    # Normalize to 0-100 scale
    normalized_score = min(100, risk_score)
    
    # Determine risk level
    if normalized_score >= 70:
        risk_level = 'critical'
    elif normalized_score >= 50:
        risk_level = 'high'
    elif normalized_score >= 25:
        risk_level = 'medium'
    else:
        risk_level = 'low'
    
    # Identify risk factors
    risk_factors = []
    if critical_alerts > 0:
        risk_factors.append(f"{critical_alerts} critical compliance alerts")
    if high_alerts > 0:
        risk_factors.append(f"{high_alerts} high-severity alerts")
    if flagged_messages > total_messages * 0.1:
        risk_factors.append("High percentage of flagged messages")
    if high_toxicity_messages > 0:
        risk_factors.append("Toxic communications detected")
    
    return {
        'overall_risk': risk_level,
        'risk_score': round(normalized_score, 2),
        'risk_factors': risk_factors,
        'alert_breakdown': {
            'critical': critical_alerts,
            'high': high_alerts,
            'medium': medium_alerts
        },
        'message_statistics': {
            'total': total_messages,
            'flagged': flagged_messages,
            'high_toxicity': high_toxicity_messages,
            'negative_sentiment': negative_sentiment_messages
        }
    }
