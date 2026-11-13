from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone
from core.models import File, Storage
from .models import (
    ComplianceRun, RegulatoryRequirement, PolicyMapping, DSARRequest,
    DataInventory, RedactionTask, ComplianceAlert
)
from .utils import (
    extract_regulatory_requirements, analyze_policy_compliance,
    search_personal_data, redact_document_content, generate_compliance_report
)

import logging
import json

logger = logging.getLogger(__name__)
User = get_user_model()

# Practice-area AI on the platform's real search, question answering and PII detection.
from core.ai_services import semantic_search_task, ask_question_task, detect_pii_task  # noqa: E402
AI_SERVICES_AVAILABLE = True


def call_ai_regulatory_analysis(file_obj, user, regulation_type='general'):
    """
    Use AI services to analyze regulatory compliance.
    Integrates with document_search (Milvus) and grid_documents_interrogation.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            # Fallback for local development
            return _basic_regulatory_analysis(file_obj, regulation_type)

        # Use semantic search to find similar regulatory documents
        query_text = f"regulatory compliance {regulation_type} {file_obj.filename}"
        if hasattr(file_obj, 'content') and file_obj.content:
            query_text += f" {file_obj.content[:500]}"

        # Call semantic search service (internal Django app)
        search_result = semantic_search_task.delay(
            query=query_text,
            top_k=10,
            file_id=file_obj.id,
            user_id=user.id
        ).get(timeout=30)

        # Use document Q&A for intelligent regulatory analysis
        if regulation_type == 'gdpr':
            question = "Analyze this document for GDPR compliance. What personal data is processed? Are there compliance gaps?"
        elif regulation_type == 'hipaa':
            question = "Analyze this document for HIPAA compliance. What PHI is involved? Are there security requirements?"
        elif regulation_type == 'sox':
            question = "Analyze this document for SOX compliance. What financial controls and reporting requirements apply?"
        else:
            question = f"Analyze this document for {regulation_type} regulatory compliance requirements and gaps"

        qa_result = ask_question_task.delay(
            query=question,
            file_ids=[file_obj.id],
            user_id=user.id
        ).get(timeout=60)

        # Process AI results
        return _process_ai_regulatory_results(search_result, qa_result, regulation_type)

    except Exception as e:
        logger.error(f"AI regulatory analysis failed for file {file_obj.id}: {str(e)}")
        return _basic_regulatory_analysis(file_obj, regulation_type)


def call_ai_data_discovery(file_obj, user):
    """
    Use AI services to discover personal data and sensitive information.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            return []

        # Use AI PII detection service
        pii_result = detect_pii_task.delay(
            file_id=file_obj.id,
            detection_threshold=0.8
        ).get(timeout=30)

        # Use document Q&A for data classification
        question = "What types of personal data, sensitive information, or regulated data are contained in this document?"

        qa_result = ask_question_task.delay(
            query=question,
            file_ids=[file_obj.id],
            user_id=user.id
        ).get(timeout=30)

        return _process_ai_data_discovery_results(pii_result, qa_result, file_obj)

    except Exception as e:
        logger.error(f"AI data discovery failed for file {file_obj.id}: {str(e)}")
        return []


@shared_task(bind=True)
def analyze_regulatory_requirements_task(self, compliance_run_id, framework_document_ids, user_id):
    """
    Analyze regulatory framework documents to extract requirements.
    """
    try:
        user = User.objects.get(id=user_id)
        compliance_run = ComplianceRun.objects.get(id=compliance_run_id, run__user=user)
        
        logger.info(f"Starting regulatory requirements analysis for run {compliance_run_id}")
        
        # Get framework documents
        framework_documents = File.objects.filter(
            id__in=framework_document_ids,
            user=user
        )
        
        total_requirements = 0
        
        for document in framework_documents:
            try:
                # Extract requirements from document
                requirements = extract_regulatory_requirements(
                    document, 
                    compliance_run.compliance_framework
                )
                
                # Create RegulatoryRequirement objects
                for req_data in requirements:
                    requirement = RegulatoryRequirement.objects.create(
                        user=user,
                        compliance_run=compliance_run,
                        requirement_id=req_data['requirement_id'],
                        requirement_title=req_data['title'],
                        requirement_text=req_data['text'],
                        category=req_data.get('category', 'other'),
                        risk_level=req_data.get('risk_level', 'medium'),
                        compliance_status='under_review'
                    )
                    total_requirements += 1
                    
                    logger.info(f"Created requirement: {requirement.requirement_id}")
                
            except Exception as e:
                logger.error(f"Error processing document {document.id}: {str(e)}")
                continue
        
        # Store results in Storage
        Storage.objects.create(
            user=user,
            content_object=compliance_run,
            key='requirements_analysis_results',
            value={
                'total_requirements_extracted': total_requirements,
                'framework_documents_processed': len(framework_documents),
                'analysis_completed_at': timezone.now().isoformat(),
                'task_id': self.request.id
            }
        )
        
        logger.info(f"Completed regulatory requirements analysis. Extracted {total_requirements} requirements")
        
        return {
            'status': 'completed',
            'total_requirements': total_requirements,
            'documents_processed': len(framework_documents)
        }
        
    except Exception as e:
        logger.error(f"Error in analyze_regulatory_requirements_task: {str(e)}")
        raise self.retry(exc=e, countdown=60, max_retries=3)


@shared_task(bind=True)
def map_policies_to_requirements_task(self, compliance_run_id, policy_document_ids, user_id):
    """
    Map organizational policies to regulatory requirements.
    """
    try:
        user = User.objects.get(id=user_id)
        compliance_run = ComplianceRun.objects.get(id=compliance_run_id, run__user=user)
        
        logger.info(f"Starting policy mapping analysis for run {compliance_run_id}")
        
        # Get policy documents
        policy_documents = File.objects.filter(
            id__in=policy_document_ids,
            user=user
        )
        
        # Get regulatory requirements for this run
        requirements = RegulatoryRequirement.objects.filter(
            compliance_run=compliance_run,
            user=user
        )
        
        total_mappings = 0
        
        for policy_doc in policy_documents:
            try:
                # Analyze policy compliance against requirements
                policy_analysis = analyze_policy_compliance(
                    policy_doc, 
                    requirements,
                    compliance_run.compliance_framework
                )
                
                # Create PolicyMapping objects
                for mapping_data in policy_analysis['mappings']:
                    requirement = requirements.get(id=mapping_data['requirement_id'])
                    
                    mapping = PolicyMapping.objects.create(
                        user=user,
                        compliance_run=compliance_run,
                        regulatory_requirement=requirement,
                        policy_document=policy_doc,
                        policy_name=mapping_data['policy_name'],
                        policy_section=mapping_data.get('policy_section', ''),
                        mapping_strength=mapping_data['mapping_strength'],
                        gap_analysis=mapping_data.get('gap_analysis', ''),
                        recommendations=mapping_data.get('recommendations', []),
                        mapping_confidence=mapping_data.get('confidence', 0.0)
                    )
                    total_mappings += 1
                    
                    logger.info(f"Created policy mapping: {mapping.policy_name} -> {requirement.requirement_id}")
                
            except Exception as e:
                logger.error(f"Error processing policy document {policy_doc.id}: {str(e)}")
                continue
        
        # Store results in Storage
        Storage.objects.create(
            user=user,
            content_object=compliance_run,
            key='policy_mapping_results',
            value={
                'total_mappings_created': total_mappings,
                'policy_documents_processed': len(policy_documents),
                'requirements_analyzed': requirements.count(),
                'analysis_completed_at': timezone.now().isoformat(),
                'task_id': self.request.id
            }
        )
        
        logger.info(f"Completed policy mapping analysis. Created {total_mappings} mappings")
        
        return {
            'status': 'completed',
            'total_mappings': total_mappings,
            'documents_processed': len(policy_documents)
        }
        
    except Exception as e:
        logger.error(f"Error in map_policies_to_requirements_task: {str(e)}")
        raise self.retry(exc=e, countdown=60, max_retries=3)


@shared_task(bind=True)
def process_dsar_request_task(self, dsar_request_id, user_id):
    """
    Process a Data Subject Access Request (DSAR).
    """
    try:
        user = User.objects.get(id=user_id)
        dsar_request = DSARRequest.objects.get(id=dsar_request_id, user=user)
        
        logger.info(f"Starting DSAR processing for request {dsar_request.request_id}")
        
        # Update status to in_progress
        dsar_request.status = 'in_progress'
        dsar_request.save()
        
        # Search for personal data across data sources
        data_sources = [
            'user_database',
            'document_storage',
            'email_systems',
            'backup_systems',
            'log_files'
        ]
        
        personal_data_found = False
        data_categories = []
        search_results = {}
        
        for data_source in data_sources:
            try:
                # Search for personal data in each source
                search_result = search_personal_data(
                    data_source,
                    dsar_request.data_subject_email,
                    dsar_request.data_subject_name,
                    dsar_request.data_subject_id
                )
                
                search_results[data_source] = search_result
                
                if search_result['data_found']:
                    personal_data_found = True
                    data_categories.extend(search_result['data_categories'])
                
                logger.info(f"Searched {data_source}: {search_result['records_found']} records found")
                
            except Exception as e:
                logger.error(f"Error searching {data_source}: {str(e)}")
                search_results[data_source] = {'error': str(e)}
                continue
        
        # Remove duplicates from data categories
        data_categories = list(set(data_categories))
        
        # Update DSAR request with results
        dsar_request.data_sources_searched = data_sources
        dsar_request.personal_data_found = personal_data_found
        dsar_request.data_categories = data_categories
        dsar_request.status = 'data_collection'
        dsar_request.processing_notes = f"Data search completed. Found data in {len([r for r in search_results.values() if r.get('data_found', False)])} sources."
        dsar_request.save()
        
        # Store detailed search results in Storage
        Storage.objects.create(
            user=user,
            content_object=dsar_request,
            key='dsar_search_results',
            value={
                'search_results': search_results,
                'total_records_found': sum([r.get('records_found', 0) for r in search_results.values()]),
                'search_completed_at': timezone.now().isoformat(),
                'task_id': self.request.id
            }
        )
        
        # Create compliance alert if no data found (potential issue)
        if not personal_data_found:
            ComplianceAlert.objects.create(
                user=user,
                compliance_run=dsar_request.compliance_run,
                alert_type='dsar_no_data',
                alert_title=f"No Personal Data Found for DSAR {dsar_request.request_id}",
                alert_description=f"DSAR search for {dsar_request.data_subject_name} ({dsar_request.data_subject_email}) found no personal data. This may indicate incomplete search or data subject not in systems.",
                severity='medium',
                priority='medium',
                related_dsar=dsar_request,
                status='open'
            )
        
        logger.info(f"Completed DSAR processing for request {dsar_request.request_id}")
        
        return {
            'status': 'completed',
            'personal_data_found': personal_data_found,
            'data_categories': data_categories,
            'sources_searched': len(data_sources)
        }
        
    except Exception as e:
        logger.error(f"Error in process_dsar_request_task: {str(e)}")
        # Update DSAR status to failed
        try:
            dsar_request = DSARRequest.objects.get(id=dsar_request_id)
            dsar_request.status = 'failed'
            dsar_request.processing_notes = f"Processing failed: {str(e)}"
            dsar_request.save()
        except:
            pass
        raise self.retry(exc=e, countdown=60, max_retries=3)


@shared_task(bind=True)
def perform_document_redaction_task(self, redaction_task_id, user_id):
    """
    Perform document redaction for privacy protection.
    """
    try:
        user = User.objects.get(id=user_id)
        redaction_task = RedactionTask.objects.get(id=redaction_task_id, user=user)
        
        logger.info(f"Starting document redaction for task {redaction_task.task_name}")
        
        # Update status to processing
        redaction_task.status = 'processing'
        redaction_task.save()
        
        # Perform document redaction
        redaction_result = redact_document_content(
            redaction_task.source_document,
            redaction_task.redaction_type,
            redaction_task.redaction_rules,
            redaction_task.redaction_patterns
        )
        
        # Update redaction task with results
        redaction_task.redaction_count = redaction_result['redaction_count']
        redaction_task.redaction_summary = redaction_result['redaction_summary']
        redaction_task.processing_metadata = redaction_result['processing_metadata']
        
        # Create redacted document file if redaction was successful
        if redaction_result['success']:
            # Create new File object for redacted document
            redacted_file = File.objects.create(
                user=user,
                filename=f"redacted_{redaction_task.source_document.filename}",
                file_path=redaction_result['redacted_file_path'],
                file_size=redaction_result['redacted_file_size'],
                mime_type=redaction_task.source_document.mime_type
            )
            
            redaction_task.redacted_document = redacted_file
            redaction_task.status = 'completed' if not redaction_task.qa_required else 'review_required'
        else:
            redaction_task.status = 'failed'
            redaction_task.processing_metadata['error'] = redaction_result.get('error', 'Unknown error')
        
        redaction_task.save()
        
        logger.info(f"Completed document redaction for task {redaction_task.task_name}")
        
        return {
            'status': 'completed',
            'redaction_count': redaction_result['redaction_count'],
            'success': redaction_result['success']
        }
        
    except Exception as e:
        logger.error(f"Error in perform_document_redaction_task: {str(e)}")
        # Update redaction task status to failed
        try:
            redaction_task = RedactionTask.objects.get(id=redaction_task_id)
            redaction_task.status = 'failed'
            redaction_task.processing_metadata = {'error': str(e)}
            redaction_task.save()
        except:
            pass
        raise self.retry(exc=e, countdown=60, max_retries=3)


@shared_task(bind=True)
def generate_compliance_report_task(self, compliance_run_id, report_type, include_sections, user_id):
    """
    Generate comprehensive compliance reports.
    """
    try:
        user = User.objects.get(id=user_id)
        compliance_run = ComplianceRun.objects.get(id=compliance_run_id, run__user=user)

        logger.info(f"Starting compliance report generation for run {compliance_run_id}")

        # Generate compliance report
        report_result = generate_compliance_report(
            compliance_run,
            report_type,
            include_sections
        )

        # Create File object for the generated report
        report_file = File.objects.create(
            user=user,
            filename=f"compliance_report_{compliance_run.id}_{report_type}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.pdf",
            file_path=report_result['report_file_path'],
            file_size=report_result['report_file_size'],
            mime_type='application/pdf'
        )

        # Store report metadata in Storage
        Storage.objects.create(
            user=user,
            content_object=compliance_run,
            key=f'compliance_report_{report_type}',
            value={
                'report_file_id': report_file.id,
                'report_type': report_type,
                'include_sections': include_sections,
                'report_metadata': report_result['metadata'],
                'generated_at': timezone.now().isoformat(),
                'task_id': self.request.id
            }
        )

        logger.info(f"Completed compliance report generation for run {compliance_run_id}")

        return {
            'status': 'completed',
            'report_file_id': report_file.id,
            'report_type': report_type
        }

    except Exception as e:
        logger.error(f"Error in generate_compliance_report_task: {str(e)}")
        raise self.retry(exc=e, countdown=60, max_retries=3)


# ═══════════════════════════════════════════════════════════════
# 🔧 HELPER FUNCTIONS FOR AI INTEGRATION
# ═══════════════════════════════════════════════════════════════

def _basic_regulatory_analysis(file_obj, regulation_type):
    """Basic regulatory analysis fallback for local development."""
    filename_lower = file_obj.filename.lower()

    # Basic analysis based on filename and regulation type
    analysis_result = {
        'regulation_type': regulation_type,
        'compliance_status': 'unknown',
        'risk_level': 'medium',
        'confidence': 0.3,
        'findings': []
    }

    # Basic keyword detection for regulatory content
    if hasattr(file_obj, 'content') and file_obj.content:
        content_lower = file_obj.content.lower()

        if regulation_type == 'gdpr':
            keywords = ['personal data', 'data subject', 'consent', 'privacy']
        elif regulation_type == 'hipaa':
            keywords = ['phi', 'protected health', 'medical', 'patient']
        elif regulation_type == 'sox':
            keywords = ['financial', 'controls', 'audit', 'reporting']
        else:
            keywords = ['compliance', 'regulation', 'requirement', 'policy']

        for keyword in keywords:
            if keyword in content_lower:
                analysis_result['findings'].append(f"Found {regulation_type.upper()} element: {keyword}")

    return analysis_result


def _process_ai_regulatory_results(search_result, qa_result, regulation_type):
    """Process AI regulatory analysis results."""
    return {
        'regulation_type': regulation_type,
        'similar_documents': search_result.get('results', [])[:5] if search_result else [],
        'ai_insights': qa_result.get('answer', '') if qa_result else '',
        'compliance_gaps': [],
        'confidence': qa_result.get('confidence', 0.5) if qa_result else 0.5
    }


def _process_ai_data_discovery_results(pii_result, qa_result, file_obj):
    """Process AI data discovery results."""
    discovered_data = []

    # Process PII detection results
    if pii_result and 'detections' in pii_result:
        for detection in pii_result['detections']:
            discovered_data.append({
                'data_type': detection.get('type', 'unknown'),
                'text': detection.get('text', ''),
                'confidence': detection.get('confidence', 0.0),
                'location': detection.get('location', {}),
                'source': 'pii_detection'
            })

    # Process Q&A results
    if qa_result and 'answer' in qa_result:
        discovered_data.append({
            'data_type': 'ai_analysis',
            'description': qa_result['answer'],
            'confidence': qa_result.get('confidence', 0.5),
            'source': 'document_qa'
        })

    return {
        'file': file_obj.filename,
        'discovered_data': discovered_data,
        'total_findings': len(discovered_data),
        'confidence': 0.7
    }
