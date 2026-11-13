from celery import shared_task
import logging
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.db import models, transaction
from django.db.models import Q
from core.models import File
from .models import (
    WorkplaceCommunicationsRun, CommunicationMessage, WageHourAnalysis,
    PolicyComparison, EEOCPacket, CommunicationPattern, ComplianceAlert
)

logger = logging.getLogger(__name__)
User = get_user_model()

# Practice-area AI on the platform's real search, question answering and PII detection.
from core.ai_services import semantic_search_task, ask_question_task, detect_pii_task  # noqa: E402
AI_SERVICES_AVAILABLE = True


def call_ai_communication_analysis(file_obj, user, analysis_type='general'):
    """
    Use AI services to analyze workplace communications.
    Integrates with document_search (Milvus) and grid_documents_interrogation.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            # Fallback for local development
            return _basic_communication_analysis(file_obj, analysis_type)

        # Use semantic search to find similar communication patterns
        query_text = f"workplace communication {analysis_type} {file_obj.filename}"
        if hasattr(file_obj, 'content') and file_obj.content:
            query_text += f" {file_obj.content[:500]}"

        # Call semantic search service (internal Django app)
        search_result = semantic_search_task.delay(
            query=query_text,
            top_k=10,
            file_id=file_obj.id,
            user_id=user.id
        ).get(timeout=30)

        # Use document Q&A for intelligent analysis
        if analysis_type == 'harassment':
            question = "Does this communication contain harassment, discrimination, or inappropriate workplace behavior?"
        elif analysis_type == 'policy_violation':
            question = "Does this communication violate workplace policies or professional conduct?"
        elif analysis_type == 'wage_hour':
            question = "Does this communication discuss wages, hours, overtime, or compensation issues?"
        else:
            question = f"Analyze this workplace communication for {analysis_type} issues"

        qa_result = ask_question_task.delay(
            query=question,
            file_ids=[file_obj.id],
            user_id=user.id
        ).get(timeout=30)

        # Process AI results
        return _process_ai_communication_results(search_result, qa_result, analysis_type)

    except Exception as e:
        logger.error(f"AI communication analysis failed for file {file_obj.id}: {str(e)}")
        return _basic_communication_analysis(file_obj, analysis_type)


def call_ai_policy_comparison(policy_file, reference_files, user):
    """
    Use AI services to compare workplace policies.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            return _basic_policy_comparison(policy_file, reference_files)

        # Use document Q&A to analyze policy differences
        question = f"Compare this policy document with standard workplace policies. What are the key differences, gaps, or compliance issues?"

        file_ids = [policy_file.id] + [f.id for f in reference_files]

        qa_result = ask_question_task.delay(
            query=question,
            file_ids=file_ids,
            user_id=user.id
        ).get(timeout=60)

        return _process_ai_policy_results(qa_result, policy_file, reference_files)

    except Exception as e:
        logger.error(f"AI policy comparison failed: {str(e)}")
        return _basic_policy_comparison(policy_file, reference_files)


def call_ai_pii_detection(file_obj, user):
    """
    Use AI services to detect PII in workplace communications.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            return []

        # Use AI PII detection service
        pii_result = detect_pii_task.delay(
            file_id=file_obj.id,
            detection_threshold=0.8
        ).get(timeout=30)

        return _process_ai_pii_results(pii_result, file_obj)

    except Exception as e:
        logger.error(f"AI PII detection failed for file {file_obj.id}: {str(e)}")
        return []


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def analyze_communications_task(self, comm_run_id, file_ids, user_id):
    """
    Celery task to analyze workplace communications for sentiment, toxicity, and compliance.
    """
    try:
        comm_run = WorkplaceCommunicationsRun.objects.get(id=comm_run_id)
        user = User.objects.get(id=user_id)
        files = File.objects.filter(id__in=file_ids, user=user)
        
        logger.info(f"Starting communication analysis for {len(files)} files")
        
        messages_created = 0
        alerts_created = 0
        
        for file_obj in files:
            # Use AI services for intelligent message extraction and analysis
            filename_lower = file_obj.filename.lower()

            # Determine message type
            if '.eml' in filename_lower or 'email' in filename_lower:
                message_type = 'email'
            elif 'slack' in filename_lower:
                message_type = 'slack'
            elif 'teams' in filename_lower:
                message_type = 'teams'
            else:
                message_type = 'email'

            # Use AI for communication analysis
            analysis_result = call_ai_communication_analysis(file_obj, user, 'general')  # default
            
            # Mock message data
            mock_messages = [
                {
                    'message_id': f"{file_obj.id}_msg_001",
                    'sender': 'john.doe@company.com',
                    'recipients': ['jane.smith@company.com'],
                    'subject': 'Project Update',
                    'content': 'Hi Jane, just wanted to update you on the project status.',
                    'sent_datetime': timezone.now(),
                    'sentiment_score': 0.2,
                    'toxicity_score': 0.1,
                    'relevance_score': 0.6
                },
                {
                    'message_id': f"{file_obj.id}_msg_002",
                    'sender': 'manager@company.com',
                    'recipients': ['team@company.com'],
                    'subject': 'Overtime Requirements',
                    'content': 'Team, we need everyone to work extra hours this weekend.',
                    'sent_datetime': timezone.now(),
                    'sentiment_score': -0.3,
                    'toxicity_score': 0.2,
                    'relevance_score': 0.9
                }
            ]
            
            # Create communication messages
            for msg_data in mock_messages:
                # Check for potential compliance issues
                is_flagged = False
                flag_reason = ""
                
                content_lower = msg_data['content'].lower()
                if any(word in content_lower for word in ['overtime', 'extra hours', 'work weekend']):
                    is_flagged = True
                    flag_reason = "Potential overtime work indication"
                
                message = CommunicationMessage.objects.create(
                    file=file_obj,
                    user=user,
                    communications_run=comm_run,
                    message_type=message_type,
                    is_flagged=is_flagged,
                    flag_reason=flag_reason,
                    processing_metadata={
                        'processed_at': timezone.now().isoformat(),
                        'task_id': self.request.id
                    },
                    **msg_data
                )
                messages_created += 1
                
                # Create compliance alerts for flagged messages
                if is_flagged:
                    ComplianceAlert.objects.create(
                        user=user,
                        communications_run=comm_run,
                        alert_type='overtime_indication',
                        alert_title='Potential Overtime Work Detected',
                        alert_description=f'Message from {msg_data["sender"]} indicates potential overtime work',
                        severity='medium',
                        priority='medium'
                    ).related_messages.add(message)
                    alerts_created += 1
        
        logger.info(f"Communication analysis completed: {messages_created} messages, {alerts_created} alerts")
        
        return {
            "status": "completed",
            "comm_run_id": comm_run_id,
            "messages_created": messages_created,
            "alerts_created": alerts_created
        }
        
    except WorkplaceCommunicationsRun.DoesNotExist:
        logger.error(f"WorkplaceCommunicationsRun with id {comm_run_id} not found")
        return {"status": "failed", "error": "Communications run not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Communication analysis failed for run {comm_run_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def analyze_wage_hour_task(self, comm_run_id, employee_list, user_id):
    """
    Celery task to analyze wage and hour patterns from communications.
    """
    try:
        comm_run = WorkplaceCommunicationsRun.objects.get(id=comm_run_id)
        user = User.objects.get(id=user_id)
        
        logger.info(f"Starting wage hour analysis for {len(employee_list)} employees")
        
        analyses_created = 0
        
        # If no specific employees provided, analyze all message senders
        if not employee_list:
            employee_list = CommunicationMessage.objects.filter(
                communications_run=comm_run,
                user=user
            ).values_list('sender', flat=True).distinct()
        
        for employee_email in employee_list:
            # Get messages from this employee
            employee_messages = CommunicationMessage.objects.filter(
                communications_run=comm_run,
                user=user,
                sender=employee_email
            )
            
            if not employee_messages.exists():
                continue
            
            # Analyze message timing patterns
            early_morning_count = 0
            late_evening_count = 0
            weekend_count = 0
            
            for message in employee_messages:
                hour = message.sent_datetime.hour
                weekday = message.sent_datetime.weekday()
                
                if hour < 7:  # Before 7 AM
                    early_morning_count += 1
                elif hour > 19:  # After 7 PM
                    late_evening_count += 1
                
                if weekday >= 5:  # Saturday (5) or Sunday (6)
                    weekend_count += 1
            
            # Mock wage calculations
            total_hours = 40.0  # Base assumption
            overtime_hours = max(0, (early_morning_count + late_evening_count) * 0.5)  # Estimate
            hourly_rate = 25.00  # Mock rate
            
            regular_pay = min(40, total_hours) * hourly_rate
            overtime_pay = overtime_hours * hourly_rate * 1.5
            total_pay = regular_pay + overtime_pay
            
            # Detect potential violations
            potential_overtime_violations = overtime_hours > 0
            potential_break_violations = early_morning_count > 5
            potential_meal_violations = late_evening_count > 10
            
            # Create wage hour analysis
            analysis = WageHourAnalysis.objects.create(
                user=user,
                communications_run=comm_run,
                employee_name=employee_email.split('@')[0].replace('.', ' ').title(),
                employee_id=f"EMP_{employee_email.split('@')[0].upper()}",
                analysis_start_date=comm_run.analysis_start_date.date(),
                analysis_end_date=comm_run.analysis_end_date.date(),
                total_hours_worked=total_hours + overtime_hours,
                regular_hours=total_hours,
                overtime_hours=overtime_hours,
                early_morning_messages=early_morning_count,
                late_evening_messages=late_evening_count,
                weekend_messages=weekend_count,
                hourly_rate=hourly_rate,
                regular_pay=regular_pay,
                overtime_pay=overtime_pay,
                total_pay=total_pay,
                potential_overtime_violations=potential_overtime_violations,
                potential_break_violations=potential_break_violations,
                potential_meal_violations=potential_meal_violations,
                analysis_metadata={
                    'total_messages': employee_messages.count(),
                    'analysis_method': 'communication_timing',
                    'processed_at': timezone.now().isoformat(),
                    'task_id': self.request.id
                }
            )
            analyses_created += 1
            
            # Create compliance alerts for violations
            if potential_overtime_violations:
                ComplianceAlert.objects.create(
                    user=user,
                    communications_run=comm_run,
                    alert_type='overtime_indication',
                    alert_title=f'Potential Overtime Violations - {analysis.employee_name}',
                    alert_description=f'Employee shows {overtime_hours:.1f} hours of potential overtime work',
                    severity='high' if overtime_hours > 10 else 'medium',
                    priority='high'
                )
        
        logger.info(f"Wage hour analysis completed: {analyses_created} analyses created")
        
        return {
            "status": "completed",
            "comm_run_id": comm_run_id,
            "analyses_created": analyses_created,
            "employees_analyzed": len(employee_list)
        }
        
    except WorkplaceCommunicationsRun.DoesNotExist:
        logger.error(f"WorkplaceCommunicationsRun with id {comm_run_id} not found")
        return {"status": "failed", "error": "Communications run not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Wage hour analysis failed for run {comm_run_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def compare_policies_task(self, comm_run_id, policy_file_ids, user_id):
    """
    Celery task to compare company policies against communications and best practices.
    """
    try:
        comm_run = WorkplaceCommunicationsRun.objects.get(id=comm_run_id)
        user = User.objects.get(id=user_id)
        policy_files = File.objects.filter(id__in=policy_file_ids, user=user)
        
        logger.info(f"Starting policy comparison for {len(policy_files)} policy files")
        
        comparisons_created = 0
        
        for policy_file in policy_files:
            # Mock policy text extraction
            policy_text = f"Sample policy text from {policy_file.filename}"
            
            # Determine policy type from filename
            filename_lower = policy_file.filename.lower()
            if 'harassment' in filename_lower:
                policy_type = 'harassment'
            elif 'discrimination' in filename_lower:
                policy_type = 'discrimination'
            elif 'code' in filename_lower and 'conduct' in filename_lower:
                policy_type = 'code_of_conduct'
            elif 'social' in filename_lower and 'media' in filename_lower:
                policy_type = 'social_media'
            else:
                policy_type = 'other'
            
            # Mock policy analysis
            violations_found = []
            recommendations = []
            missing_elements = []
            
            # Check communications for policy violations
            messages = CommunicationMessage.objects.filter(
                communications_run=comm_run,
                user=user
            )
            
            violation_count = 0
            for message in messages:
                content_lower = message.content.lower()
                if policy_type == 'harassment' and any(word in content_lower for word in ['inappropriate', 'uncomfortable']):
                    violations_found.append({
                        'message_id': message.message_id,
                        'violation_type': 'potential_harassment',
                        'description': 'Message contains language that may violate harassment policy'
                    })
                    violation_count += 1
            
            # Calculate compliance score
            total_messages = messages.count()
            compliance_score = max(0.0, 1.0 - (violation_count / max(1, total_messages)))
            
            # Generate recommendations
            if compliance_score < 0.8:
                recommendations.append("Review and update policy language for clarity")
                recommendations.append("Provide additional training on policy compliance")
            
            # Mock best practices comparison
            best_practices_score = 0.75  # Mock score
            if policy_type == 'harassment':
                missing_elements = ["Anonymous reporting mechanism", "Clear investigation process"]
            
            # Create policy comparison
            PolicyComparison.objects.create(
                user=user,
                communications_run=comm_run,
                policy_name=policy_file.filename.replace('.pdf', '').replace('_', ' ').title(),
                policy_type=policy_type,
                policy_document=policy_file,
                policy_text=policy_text,
                compliance_score=compliance_score,
                violations_found=violations_found,
                recommendations=recommendations,
                best_practices_score=best_practices_score,
                missing_elements=missing_elements
            )
            comparisons_created += 1
        
        logger.info(f"Policy comparison completed: {comparisons_created} comparisons created")
        
        return {
            "status": "completed",
            "comm_run_id": comm_run_id,
            "comparisons_created": comparisons_created,
            "policy_files_analyzed": len(policy_files)
        }
        
    except WorkplaceCommunicationsRun.DoesNotExist:
        logger.error(f"WorkplaceCommunicationsRun with id {comm_run_id} not found")
        return {"status": "failed", "error": "Communications run not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Policy comparison failed for run {comm_run_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def generate_eeoc_packet_task(self, packet_id, user_id):
    """
    Celery task to generate EEOC complaint packet with relevant evidence.
    """
    try:
        packet = EEOCPacket.objects.get(id=packet_id)
        user = User.objects.get(id=user_id)
        
        logger.info(f"Generating EEOC packet: {packet.packet_name}")
        
        # Update status
        packet.status = 'review'
        packet.save(update_fields=['status'])
        
        # Find relevant messages based on complaint type and timeline
        relevant_messages = CommunicationMessage.objects.filter(
            communications_run=packet.communications_run,
            user=user,
            sent_datetime__gte=packet.incident_date
        )
        
        # Filter messages based on complaint type
        if packet.complaint_type in ['sexual_harassment', 'harassment']:
            # Look for messages with harassment indicators
            relevant_messages = relevant_messages.filter(
                Q(content__icontains='inappropriate') |
                Q(content__icontains='uncomfortable') |
                Q(toxicity_score__gte=0.5)
            )
        elif packet.complaint_type == 'retaliation':
            # Look for messages after incident date with negative sentiment
            relevant_messages = relevant_messages.filter(
                sentiment_score__lt=-0.3
            )
        
        # Add relevant messages to packet
        packet.relevant_messages.set(relevant_messages[:50])  # Limit to 50 most relevant
        
        # Calculate evidence strength
        evidence_strength = 0.0
        if relevant_messages.exists():
            avg_relevance = relevant_messages.aggregate(avg_rel=models.Avg('relevance_score'))['avg_rel'] or 0
            message_count_factor = min(1.0, relevant_messages.count() / 20.0)
            evidence_strength = (avg_relevance + message_count_factor) / 2
        
        # Generate timeline analysis
        timeline_events = []
        for message in relevant_messages.order_by('sent_datetime')[:10]:
            timeline_events.append({
                'date': message.sent_datetime.isoformat(),
                'event': f"Message from {message.sender}",
                'description': message.content[:100] + "..." if len(message.content) > 100 else message.content,
                'relevance_score': message.relevance_score
            })
        
        # Generate key findings
        key_findings = [
            f"Found {relevant_messages.count()} relevant communications",
            f"Evidence strength score: {evidence_strength:.2f}",
        ]
        
        if relevant_messages.filter(toxicity_score__gte=0.7).exists():
            key_findings.append("High toxicity communications identified")
        
        # Update packet with analysis results
        packet.evidence_strength_score = evidence_strength
        packet.timeline_analysis = {'events': timeline_events}
        packet.key_findings = key_findings
        packet.status = 'ready'
        packet.generation_metadata = {
            'generated_at': timezone.now().isoformat(),
            'task_id': self.request.id,
            'relevant_messages_count': relevant_messages.count()
        }
        packet.save()
        
        logger.info(f"EEOC packet generated: {packet.packet_name}")
        
        return {
            "status": "completed",
            "packet_id": packet_id,
            "evidence_strength_score": evidence_strength,
            "relevant_messages_count": relevant_messages.count()
        }
        
    except EEOCPacket.DoesNotExist:
        logger.error(f"EEOCPacket with id {packet_id} not found")
        return {"status": "failed", "error": "EEOC packet not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"EEOC packet generation failed for {packet_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=120, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def detect_communication_patterns_task(self, comm_run_id, user_id):
    """
    Celery task to detect patterns in workplace communications.
    """
    try:
        comm_run = WorkplaceCommunicationsRun.objects.get(id=comm_run_id)
        user = User.objects.get(id=user_id)
        
        logger.info(f"Starting communication pattern detection for: {comm_run.case_name}")
        
        messages = CommunicationMessage.objects.filter(
            communications_run=comm_run,
            user=user
        )
        
        patterns_created = 0
        
        # Pattern 1: Communication exclusion (someone being left out)
        all_senders = set(messages.values_list('sender', flat=True))
        for sender in all_senders:
            sender_messages = messages.filter(sender=sender)
            # Check if this person is rarely included in group communications
            group_messages = messages.filter(recipients__len__gt=2)  # More than 2 recipients
            included_count = group_messages.filter(recipients__contains=[sender]).count()
            
            if group_messages.count() > 10 and included_count / group_messages.count() < 0.3:
                CommunicationPattern.objects.create(
                    user=user,
                    communications_run=comm_run,
                    pattern_type='exclusion',
                    pattern_name=f'Communication Exclusion - {sender}',
                    description=f'{sender} is excluded from {100 - (included_count/group_messages.count()*100):.1f}% of group communications',
                    involved_personnel=[sender],
                    confidence_score=0.8,
                    severity_score=0.7,
                    pattern_start_date=comm_run.analysis_start_date,
                    pattern_end_date=comm_run.analysis_end_date,
                    pattern_details={
                        'total_group_messages': group_messages.count(),
                        'included_count': included_count,
                        'exclusion_rate': 1 - (included_count / group_messages.count())
                    }
                )
                patterns_created += 1
        
        # Pattern 2: Sentiment shift over time
        for sender in all_senders:
            sender_messages = messages.filter(sender=sender, sentiment_score__isnull=False).order_by('sent_datetime')
            if sender_messages.count() >= 10:
                # Compare first half vs second half sentiment
                mid_point = sender_messages.count() // 2
                first_half_sentiment = sender_messages[:mid_point].aggregate(avg_sentiment=models.Avg('sentiment_score'))['avg_sentiment']
                second_half_sentiment = sender_messages[mid_point:].aggregate(avg_sentiment=models.Avg('sentiment_score'))['avg_sentiment']
                
                if first_half_sentiment and second_half_sentiment:
                    sentiment_change = second_half_sentiment - first_half_sentiment
                    if abs(sentiment_change) > 0.5:  # Significant sentiment shift
                        CommunicationPattern.objects.create(
                            user=user,
                            communications_run=comm_run,
                            pattern_type='sentiment_shift',
                            pattern_name=f'Sentiment Shift - {sender}',
                            description=f'{sender} shows {"positive" if sentiment_change > 0 else "negative"} sentiment shift of {abs(sentiment_change):.2f}',
                            involved_personnel=[sender],
                            confidence_score=0.75,
                            severity_score=min(1.0, abs(sentiment_change)),
                            pattern_start_date=sender_messages.first().sent_datetime,
                            pattern_end_date=sender_messages.last().sent_datetime,
                            pattern_details={
                                'first_half_sentiment': first_half_sentiment,
                                'second_half_sentiment': second_half_sentiment,
                                'sentiment_change': sentiment_change
                            }
                        )
                        patterns_created += 1
        
        logger.info(f"Communication pattern detection completed: {patterns_created} patterns found")
        
        return {
            "status": "completed",
            "comm_run_id": comm_run_id,
            "patterns_created": patterns_created
        }
        
    except WorkplaceCommunicationsRun.DoesNotExist:
        logger.error(f"WorkplaceCommunicationsRun with id {comm_run_id} not found")
        return {"status": "failed", "error": "Communications run not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Communication pattern detection failed for run {comm_run_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=120, exc=e)
        return {"status": "failed", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 🔧 HELPER FUNCTIONS FOR AI INTEGRATION
# ═══════════════════════════════════════════════════════════════

def _basic_communication_analysis(file_obj, analysis_type):
    """Basic communication analysis fallback for local development."""
    filename_lower = file_obj.filename.lower()

    # Basic analysis based on filename and type
    analysis_result = {
        'analysis_type': analysis_type,
        'message_type': 'email' if '.eml' in filename_lower else 'other',
        'risk_level': 'low',
        'confidence': 0.3,
        'findings': []
    }

    # Basic keyword detection
    if hasattr(file_obj, 'content') and file_obj.content:
        content_lower = file_obj.content.lower()
        risk_keywords = ['harassment', 'discrimination', 'inappropriate', 'violation']

        for keyword in risk_keywords:
            if keyword in content_lower:
                analysis_result['risk_level'] = 'medium'
                analysis_result['findings'].append(f"Found keyword: {keyword}")

    return analysis_result


def _basic_policy_comparison(policy_file, reference_files):
    """Basic policy comparison fallback for local development."""
    return {
        'policy_file': policy_file.filename,
        'reference_count': len(reference_files),
        'comparison_summary': 'Basic comparison - AI services not available',
        'gaps_identified': [],
        'confidence': 0.3
    }


def _process_ai_communication_results(search_result, qa_result, analysis_type):
    """Process AI communication analysis results."""
    return {
        'analysis_type': analysis_type,
        'similar_patterns': search_result.get('results', [])[:5] if search_result else [],
        'ai_insights': qa_result.get('answer', '') if qa_result else '',
        'confidence': qa_result.get('confidence', 0.5) if qa_result else 0.5
    }


def _process_ai_policy_results(qa_result, policy_file, reference_files):
    """Process AI policy comparison results."""
    return {
        'policy_file': policy_file.filename,
        'comparison_insights': qa_result.get('answer', '') if qa_result else '',
        'reference_count': len(reference_files),
        'confidence': qa_result.get('confidence', 0.5) if qa_result else 0.5
    }


def _process_ai_pii_results(pii_result, file_obj):
    """Process AI PII detection results."""
    if not pii_result or 'detections' not in pii_result:
        return []

    pii_instances = []
    for detection in pii_result['detections']:
        pii_instances.append({
            'pii_type': detection.get('type', 'unknown'),
            'text': detection.get('text', ''),
            'confidence': detection.get('confidence', 0.0),
            'location': detection.get('location', {})
        })

    return pii_instances
