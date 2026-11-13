from celery import shared_task
import logging
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.db import transaction
from core.models import File
from .models import (
    PatentAnalysisRun, PatentDocument, PatentClaim, PriorArtDocument,
    ClaimChart, InfringementAnalysis, ValidityChallenge
)

logger = logging.getLogger(__name__)
User = get_user_model()

# Practice-area AI on the platform's real search, question answering and PII detection.
from core.ai_services import semantic_search_task, ask_question_task, detect_pii_task  # noqa: E402
AI_SERVICES_AVAILABLE = True


def call_ai_patent_analysis(file_obj, user, analysis_type='general'):
    """
    Use AI services to analyze patent documents.
    Integrates with document_search (Milvus) and grid_documents_interrogation.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            # Fallback for local development
            return _basic_patent_analysis(file_obj, analysis_type)

        # Use semantic search to find similar patents
        query_text = f"patent {analysis_type} {file_obj.filename}"
        if hasattr(file_obj, 'content') and file_obj.content:
            query_text += f" {file_obj.content[:500]}"

        # Call semantic search service (internal Django app)
        search_result = semantic_search_task.delay(
            query=query_text,
            top_k=10,
            file_id=file_obj.id,
            user_id=user.id
        ).get(timeout=30)

        # Use document Q&A for intelligent patent analysis
        if analysis_type == 'claims':
            question = "What are the independent and dependent claims in this patent? Analyze their scope and limitations."
        elif analysis_type == 'prior_art':
            question = "What prior art references are cited in this patent? Analyze their relevance to the claims."
        elif analysis_type == 'infringement':
            question = "Analyze this patent for potential infringement issues. What are the key claim elements?"
        elif analysis_type == 'validity':
            question = "Analyze this patent for validity issues. Are there potential prior art or obviousness concerns?"
        else:
            question = f"Analyze this patent document for {analysis_type} aspects"

        qa_result = ask_question_task.delay(
            query=question,
            file_ids=[file_obj.id],
            user_id=user.id
        ).get(timeout=60)

        # Process AI results
        return _process_ai_patent_results(search_result, qa_result, analysis_type)

    except Exception as e:
        logger.error(f"AI patent analysis failed for file {file_obj.id}: {str(e)}")
        return _basic_patent_analysis(file_obj, analysis_type)


def call_ai_prior_art_search(patent_file, user, search_scope='comprehensive'):
    """
    Use AI services to search for prior art.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            return _basic_prior_art_search(patent_file, search_scope)

        # Extract key concepts from patent for prior art search
        question = "Extract the key technical concepts, innovations, and claim elements from this patent for prior art searching."

        concept_result = ask_question_task.delay(
            query=question,
            file_ids=[patent_file.id],
            user_id=user.id
        ).get(timeout=30)

        # Use semantic search to find similar prior art
        if concept_result and 'answer' in concept_result:
            search_query = f"prior art {concept_result['answer'][:200]}"

            prior_art_result = semantic_search_task.delay(
                query=search_query,
                top_k=20,
                user_id=user.id
            ).get(timeout=60)

            return _process_ai_prior_art_results(prior_art_result, patent_file)

        return []

    except Exception as e:
        logger.error(f"AI prior art search failed: {str(e)}")
        return _basic_prior_art_search(patent_file, search_scope)


def call_ai_claim_construction(patent_file, user):
    """
    Use AI services for claim construction analysis.
    """
    try:
        if not AI_SERVICES_AVAILABLE:
            return _basic_claim_construction(patent_file)

        # Use document Q&A for claim construction
        question = "Provide a detailed claim construction analysis. For each claim element, identify: 1) Plain meaning, 2) Specification support, 3) Prosecution history, 4) Potential claim scope issues."

        qa_result = ask_question_task.delay(
            query=question,
            file_ids=[patent_file.id],
            user_id=user.id
        ).get(timeout=60)

        return _process_ai_claim_construction_results(qa_result, patent_file)

    except Exception as e:
        logger.error(f"AI claim construction failed: {str(e)}")
        return _basic_claim_construction(patent_file)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def extract_patent_data_task(self, patent_document_id, user_id):
    """
    Celery task to extract patent data from patent documents.
    """
    try:
        patent_document = PatentDocument.objects.get(id=patent_document_id)
        user = User.objects.get(id=user_id)
        
        logger.info(f"Starting patent data extraction for: {patent_document.patent_number}")
        
        # Mock patent data extraction (in real implementation, parse patent PDF/XML)
        file_obj = patent_document.file
        filename_lower = file_obj.filename.lower()
        
        # Mock extracted data based on filename patterns
        if 'us' in filename_lower:
            patent_document.patent_office = 'uspto'
            patent_document.ipc_classes = ['H04L 29/06', 'G06F 21/62']
            patent_document.cpc_classes = ['H04L 63/0428', 'G06F 21/6218']
        elif 'ep' in filename_lower:
            patent_document.patent_office = 'epo'
            patent_document.ipc_classes = ['H04L 29/06', 'G06F 21/60']
            patent_document.cpc_classes = ['H04L 63/0428', 'G06F 21/602']
        
        # Mock content extraction
        patent_document.abstract = f"This patent relates to {patent_document.title.lower()} and provides improved methods for implementation."
        patent_document.claims_text = "1. A method comprising: (a) receiving data; (b) processing the data; (c) outputting results."
        patent_document.description_text = f"The present invention relates to {patent_document.title.lower()}. Background of the invention..."
        
        # Mock dates if not provided
        if not patent_document.filing_date:
            patent_document.filing_date = timezone.now().date()
        if not patent_document.publication_date:
            patent_document.publication_date = timezone.now().date()
        
        # Update processing metadata
        patent_document.processing_metadata = {
            'extracted_at': timezone.now().isoformat(),
            'task_id': self.request.id,
            'extraction_method': 'mock_extraction'
        }
        
        patent_document.save()
        
        logger.info(f"Patent data extraction completed for: {patent_document.patent_number}")
        
        return {
            "status": "completed",
            "patent_document_id": patent_document_id,
            "patent_number": patent_document.patent_number
        }
        
    except PatentDocument.DoesNotExist:
        logger.error(f"PatentDocument with id {patent_document_id} not found")
        return {"status": "failed", "error": "Patent document not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Patent data extraction failed for document {patent_document_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def analyze_patent_claims_task(self, patent_document_id, user_id):
    """
    Celery task to analyze and extract patent claims from patent documents.
    """
    try:
        patent_document = PatentDocument.objects.get(id=patent_document_id)
        user = User.objects.get(id=user_id)
        
        logger.info(f"Starting patent claim analysis for: {patent_document.patent_number}")
        
        # Mock claim extraction and analysis
        mock_claims = [
            {
                'claim_number': 1,
                'claim_text': 'A method for processing data, comprising: receiving input data; analyzing the input data using predetermined criteria; and generating output based on the analysis.',
                'claim_type': 'independent',
                'depends_on_claims': [],
                'claim_elements': [
                    'receiving input data',
                    'analyzing the input data using predetermined criteria',
                    'generating output based on the analysis'
                ]
            },
            {
                'claim_number': 2,
                'claim_text': 'The method of claim 1, wherein the predetermined criteria include security parameters.',
                'claim_type': 'dependent',
                'depends_on_claims': [1],
                'claim_elements': [
                    'the method of claim 1',
                    'predetermined criteria include security parameters'
                ]
            },
            {
                'claim_number': 3,
                'claim_text': 'The method of claim 1, wherein the output includes a security assessment report.',
                'claim_type': 'dependent',
                'depends_on_claims': [1],
                'claim_elements': [
                    'the method of claim 1',
                    'output includes a security assessment report'
                ]
            }
        ]
        
        claims_created = 0
        
        for claim_data in mock_claims:
            # Calculate complexity score based on number of elements
            element_count = len(claim_data['claim_elements'])
            complexity_score = min(1.0, element_count / 10.0)  # Normalize to 0-1
            
            claim = PatentClaim.objects.create(
                patent_document=patent_document,
                user=user,
                analysis_run=patent_document.analysis_run,
                claim_number=claim_data['claim_number'],
                claim_text=claim_data['claim_text'],
                claim_type=claim_data['claim_type'],
                depends_on_claims=claim_data['depends_on_claims'],
                claim_elements=claim_data['claim_elements'],
                element_count=element_count,
                complexity_score=complexity_score
            )
            claims_created += 1
        
        logger.info(f"Patent claim analysis completed: {claims_created} claims created")
        
        return {
            "status": "completed",
            "patent_document_id": patent_document_id,
            "claims_created": claims_created
        }
        
    except PatentDocument.DoesNotExist:
        logger.error(f"PatentDocument with id {patent_document_id} not found")
        return {"status": "failed", "error": "Patent document not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Patent claim analysis failed for document {patent_document_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def search_prior_art_task(self, prior_art_document_id, user_id):
    """
    Celery task to analyze prior art documents for relevance.
    """
    try:
        prior_art_document = PriorArtDocument.objects.get(id=prior_art_document_id)
        user = User.objects.get(id=user_id)
        
        logger.info(f"Starting prior art analysis for: {prior_art_document.document_id}")
        
        # Mock prior art analysis
        file_obj = prior_art_document.file
        filename_lower = file_obj.filename.lower()
        
        # Mock content extraction
        if not prior_art_document.content_text:
            prior_art_document.content_text = f"This document describes technology related to {prior_art_document.title}. The disclosed methods include various approaches to solving technical problems."
        
        # Mock relevance analysis
        patents_in_suit = prior_art_document.analysis_run.patents_in_suit
        relevance_score = 0.5  # Default relevance
        
        # Increase relevance based on document type and content
        if prior_art_document.document_type == 'patent':
            relevance_score += 0.2
        
        if any(keyword in prior_art_document.title.lower() for keyword in ['method', 'system', 'apparatus']):
            relevance_score += 0.1
        
        if any(keyword in prior_art_document.content_text.lower() for keyword in ['security', 'data', 'processing']):
            relevance_score += 0.1
        
        relevance_score = min(1.0, relevance_score)  # Cap at 1.0
        
        # Generate relevance explanation
        relevance_explanation = f"Document shows {relevance_score:.1%} relevance based on content analysis and technology overlap."
        
        # Mock art categories
        art_categories = ['data processing', 'security methods']
        if 'network' in prior_art_document.content_text.lower():
            art_categories.append('network security')
        
        # Update prior art document
        prior_art_document.relevance_score = relevance_score
        prior_art_document.relevance_explanation = relevance_explanation
        prior_art_document.art_categories = art_categories
        prior_art_document.save()
        
        logger.info(f"Prior art analysis completed for: {prior_art_document.document_id}")
        
        return {
            "status": "completed",
            "prior_art_document_id": prior_art_document_id,
            "relevance_score": relevance_score
        }
        
    except PriorArtDocument.DoesNotExist:
        logger.error(f"PriorArtDocument with id {prior_art_document_id} not found")
        return {"status": "failed", "error": "Prior art document not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Prior art analysis failed for document {prior_art_document_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def generate_claim_chart_task(self, claim_chart_id, user_id):
    """
    Celery task to generate detailed claim chart analysis.
    """
    try:
        claim_chart = ClaimChart.objects.get(id=claim_chart_id)
        user = User.objects.get(id=user_id)
        
        logger.info(f"Generating claim chart: {claim_chart.chart_name}")
        
        # Get the patent claim
        patent_claim = claim_chart.patent_claim
        
        # Mock claim chart generation
        element_mappings = []
        
        for i, element in enumerate(patent_claim.claim_elements, 1):
            if claim_chart.chart_type == 'infringement':
                # Map to accused product features
                mapping = {
                    'element_number': i,
                    'claim_element': element,
                    'accused_feature': f"Product feature {i} that implements {element.lower()}",
                    'mapping_strength': 'strong' if i <= 2 else 'moderate',
                    'evidence': f"Technical documentation shows implementation of {element.lower()}"
                }
            elif claim_chart.chart_type == 'invalidity':
                # Map to prior art disclosure
                mapping = {
                    'element_number': i,
                    'claim_element': element,
                    'prior_art_disclosure': f"Prior art discloses {element.lower()} in section {i}",
                    'mapping_strength': 'strong' if i <= 2 else 'weak',
                    'evidence': f"Prior art reference explicitly describes {element.lower()}"
                }
            else:
                # Non-infringement analysis
                mapping = {
                    'element_number': i,
                    'claim_element': element,
                    'product_analysis': f"Product does not implement {element.lower()}",
                    'mapping_strength': 'strong',
                    'evidence': f"Technical analysis shows absence of {element.lower()}"
                }
            
            element_mappings.append(mapping)
        
        # Determine overall conclusion based on mappings
        strong_mappings = sum(1 for m in element_mappings if m['mapping_strength'] == 'strong')
        total_elements = len(element_mappings)
        
        if claim_chart.chart_type == 'infringement':
            if strong_mappings >= total_elements * 0.8:
                overall_conclusion = 'infringes'
                confidence_score = 0.9
            elif strong_mappings >= total_elements * 0.5:
                overall_conclusion = 'infringes'
                confidence_score = 0.6
            else:
                overall_conclusion = 'does_not_infringe'
                confidence_score = 0.7
        elif claim_chart.chart_type == 'invalidity':
            if strong_mappings >= total_elements * 0.8:
                overall_conclusion = 'invalid'
                confidence_score = 0.8
            else:
                overall_conclusion = 'valid'
                confidence_score = 0.6
        else:  # non_infringement
            overall_conclusion = 'does_not_infringe'
            confidence_score = 0.8
        
        # Generate analysis notes
        analysis_notes = f"Claim chart analysis for {patent_claim.patent_document.patent_number}, Claim {patent_claim.claim_number}. "
        analysis_notes += f"Found {strong_mappings} strong mappings out of {total_elements} claim elements. "
        analysis_notes += f"Conclusion: {overall_conclusion.replace('_', ' ').title()}"
        
        # Update claim chart
        claim_chart.element_mappings = element_mappings
        claim_chart.overall_conclusion = overall_conclusion
        claim_chart.confidence_score = confidence_score
        claim_chart.analysis_notes = analysis_notes
        claim_chart.save()
        
        logger.info(f"Claim chart generated: {claim_chart.chart_name}")
        
        return {
            "status": "completed",
            "claim_chart_id": claim_chart_id,
            "overall_conclusion": overall_conclusion,
            "confidence_score": confidence_score
        }
        
    except ClaimChart.DoesNotExist:
        logger.error(f"ClaimChart with id {claim_chart_id} not found")
        return {"status": "failed", "error": "Claim chart not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Claim chart generation failed for {claim_chart_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=120, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def analyze_infringement_task(self, infringement_analysis_id, user_id):
    """
    Celery task to perform comprehensive infringement analysis.
    """
    try:
        infringement_analysis = InfringementAnalysis.objects.get(id=infringement_analysis_id)
        user = User.objects.get(id=user_id)

        logger.info(f"Starting infringement analysis: {infringement_analysis.analysis_name}")

        # Get asserted patents
        asserted_patents = infringement_analysis.asserted_patents.all()

        # Mock infringement analysis
        detailed_findings = {
            'patents_analyzed': [],
            'literal_infringement_findings': {},
            'doe_findings': {},
            'overall_assessment': ''
        }

        literal_infringement_count = 0
        doe_infringement_count = 0
        total_patents = asserted_patents.count()

        for patent in asserted_patents:
            patent_findings = {
                'patent_number': patent.patent_number,
                'claims_analyzed': [],
                'infringement_found': False,
                'doe_applicable': False
            }

            # Analyze claims for this patent
            claims = PatentClaim.objects.filter(patent_document=patent, user=user)

            for claim in claims[:3]:  # Analyze first 3 claims
                # Mock claim analysis
                claim_analysis = {
                    'claim_number': claim.claim_number,
                    'literal_infringement': False,
                    'doe_infringement': False,
                    'missing_elements': []
                }

                # Simulate analysis based on claim complexity
                if claim.complexity_score and claim.complexity_score > 0.5:
                    claim_analysis['literal_infringement'] = True
                    literal_infringement_count += 1
                    patent_findings['infringement_found'] = True
                elif claim.complexity_score and claim.complexity_score > 0.3:
                    claim_analysis['doe_infringement'] = True
                    doe_infringement_count += 1
                    patent_findings['doe_applicable'] = True
                else:
                    claim_analysis['missing_elements'] = ['Element A not found', 'Element B differs substantially']

                patent_findings['claims_analyzed'].append(claim_analysis)

            detailed_findings['patents_analyzed'].append(patent_findings)

        # Determine overall conclusions
        if literal_infringement_count > 0:
            literal_infringement = 'yes'
        elif literal_infringement_count == 0 and total_patents > 0:
            literal_infringement = 'no'
        else:
            literal_infringement = 'unclear'

        if doe_infringement_count > 0:
            doctrine_of_equivalents = 'yes'
        elif doe_infringement_count == 0 and total_patents > 0:
            doctrine_of_equivalents = 'no'
        else:
            doctrine_of_equivalents = 'not_applicable'

        # Overall infringement conclusion
        if literal_infringement == 'yes' or doctrine_of_equivalents == 'yes':
            infringement_conclusion = 'infringes'
            confidence_level = 'high'
        elif literal_infringement == 'no' and doctrine_of_equivalents == 'no':
            infringement_conclusion = 'does_not_infringe'
            confidence_level = 'high'
        else:
            infringement_conclusion = 'inconclusive'
            confidence_level = 'low'

        # Generate recommendations
        recommendations = []
        if infringement_conclusion == 'infringes':
            recommendations.append("Consider design-around options to avoid infringement")
            recommendations.append("Evaluate strength of asserted patents for potential invalidity challenges")
        elif infringement_conclusion == 'does_not_infringe':
            recommendations.append("Document non-infringement analysis for potential litigation defense")
        else:
            recommendations.append("Conduct more detailed technical analysis")
            recommendations.append("Consider obtaining expert technical opinion")

        detailed_findings['overall_assessment'] = f"Analysis of {total_patents} patents shows {infringement_conclusion}"

        # Update infringement analysis
        infringement_analysis.literal_infringement = literal_infringement
        infringement_analysis.doctrine_of_equivalents = doctrine_of_equivalents
        infringement_analysis.infringement_conclusion = infringement_conclusion
        infringement_analysis.confidence_level = confidence_level
        infringement_analysis.detailed_findings = detailed_findings
        infringement_analysis.recommendations = recommendations
        infringement_analysis.save()

        logger.info(f"Infringement analysis completed: {infringement_analysis.analysis_name}")

        return {
            "status": "completed",
            "infringement_analysis_id": infringement_analysis_id,
            "infringement_conclusion": infringement_conclusion,
            "confidence_level": confidence_level
        }

    except InfringementAnalysis.DoesNotExist:
        logger.error(f"InfringementAnalysis with id {infringement_analysis_id} not found")
        return {"status": "failed", "error": "Infringement analysis not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Infringement analysis failed for {infringement_analysis_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=120, exc=e)
        return {"status": "failed", "error": str(e)}


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def analyze_validity_task(self, validity_challenge_id, user_id):
    """
    Celery task to perform patent validity challenge analysis.
    """
    try:
        validity_challenge = ValidityChallenge.objects.get(id=validity_challenge_id)
        user = User.objects.get(id=user_id)

        logger.info(f"Starting validity challenge analysis: {validity_challenge.challenge_name}")

        # Get target patent and prior art references
        target_patent = validity_challenge.target_patent
        prior_art_refs = validity_challenge.prior_art_references.all()

        # Mock validity analysis
        detailed_analysis = {
            'target_patent': target_patent.patent_number,
            'prior_art_analyzed': [],
            'anticipation_findings': {},
            'obviousness_findings': {},
            'overall_assessment': ''
        }

        anticipation_strength = 0
        obviousness_strength = 0

        # Analyze each prior art reference
        for prior_art in prior_art_refs:
            art_analysis = {
                'document_id': prior_art.document_id,
                'relevance_score': prior_art.relevance_score or 0.5,
                'anticipation_analysis': '',
                'obviousness_contribution': ''
            }

            # Mock anticipation analysis
            if prior_art.relevance_score and prior_art.relevance_score > 0.8:
                art_analysis['anticipation_analysis'] = "Strong anticipation - discloses all claim elements"
                anticipation_strength += 3
            elif prior_art.relevance_score and prior_art.relevance_score > 0.6:
                art_analysis['anticipation_analysis'] = "Partial anticipation - discloses most claim elements"
                anticipation_strength += 2
            else:
                art_analysis['anticipation_analysis'] = "Weak anticipation - limited element disclosure"
                anticipation_strength += 1

            # Mock obviousness analysis
            if prior_art.document_type == 'patent':
                art_analysis['obviousness_contribution'] = "Provides technical teaching for obviousness combination"
                obviousness_strength += 2
            else:
                art_analysis['obviousness_contribution'] = "Provides background knowledge for obviousness analysis"
                obviousness_strength += 1

            detailed_analysis['prior_art_analyzed'].append(art_analysis)

        # Generate anticipation analysis
        if anticipation_strength >= 3:
            anticipation_analysis = "Strong anticipation case - at least one prior art reference discloses all claim elements"
        elif anticipation_strength >= 2:
            anticipation_analysis = "Moderate anticipation case - prior art shows substantial element disclosure"
        else:
            anticipation_analysis = "Weak anticipation case - limited element disclosure in prior art"

        # Generate obviousness analysis
        if obviousness_strength >= 4 and len(prior_art_refs) >= 2:
            obviousness_analysis = "Strong obviousness case - multiple prior art references provide clear teaching"
        elif obviousness_strength >= 2:
            obviousness_analysis = "Moderate obviousness case - prior art provides some technical guidance"
        else:
            obviousness_analysis = "Weak obviousness case - limited technical teaching in prior art"

        # Determine challenge strength and success likelihood
        total_strength = anticipation_strength + obviousness_strength
        max_possible_strength = len(prior_art_refs) * 5  # Max 3 for anticipation + 2 for obviousness per reference

        if max_possible_strength > 0:
            strength_ratio = total_strength / max_possible_strength
        else:
            strength_ratio = 0

        if strength_ratio >= 0.7:
            challenge_strength = 'strong'
            success_likelihood = 0.8
        elif strength_ratio >= 0.4:
            challenge_strength = 'moderate'
            success_likelihood = 0.5
        else:
            challenge_strength = 'weak'
            success_likelihood = 0.2

        detailed_analysis['anticipation_findings'] = {
            'strength_score': anticipation_strength,
            'analysis': anticipation_analysis
        }
        detailed_analysis['obviousness_findings'] = {
            'strength_score': obviousness_strength,
            'analysis': obviousness_analysis
        }
        detailed_analysis['overall_assessment'] = f"Challenge strength: {challenge_strength}, Success likelihood: {success_likelihood:.1%}"

        # Update validity challenge
        validity_challenge.anticipation_analysis = anticipation_analysis
        validity_challenge.obviousness_analysis = obviousness_analysis
        validity_challenge.challenge_strength = challenge_strength
        validity_challenge.success_likelihood = success_likelihood
        validity_challenge.detailed_analysis = detailed_analysis
        validity_challenge.save()

        logger.info(f"Validity challenge analysis completed: {validity_challenge.challenge_name}")

        return {
            "status": "completed",
            "validity_challenge_id": validity_challenge_id,
            "challenge_strength": challenge_strength,
            "success_likelihood": success_likelihood
        }

    except ValidityChallenge.DoesNotExist:
        logger.error(f"ValidityChallenge with id {validity_challenge_id} not found")
        return {"status": "failed", "error": "Validity challenge not found"}
    except User.DoesNotExist:
        logger.error(f"User with id {user_id} not found")
        return {"status": "failed", "error": "User not found"}
    except Exception as e:
        logger.error(f"Validity challenge analysis failed for {validity_challenge_id}: {str(e)}")
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=120, exc=e)
        return {"status": "failed", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 🔧 HELPER FUNCTIONS FOR AI INTEGRATION
# ═══════════════════════════════════════════════════════════════

def _basic_patent_analysis(file_obj, analysis_type):
    """Basic patent analysis fallback for local development."""
    filename_lower = file_obj.filename.lower()

    # Basic analysis based on filename and type
    analysis_result = {
        'analysis_type': analysis_type,
        'patent_type': 'utility' if 'utility' in filename_lower else 'unknown',
        'complexity': 'medium',
        'confidence': 0.3,
        'findings': []
    }

    # Basic keyword detection for patent content
    if hasattr(file_obj, 'content') and file_obj.content:
        content_lower = file_obj.content.lower()
        patent_keywords = ['claim', 'invention', 'embodiment', 'prior art', 'specification']

        for keyword in patent_keywords:
            if keyword in content_lower:
                analysis_result['findings'].append(f"Found patent element: {keyword}")

    return analysis_result


def _basic_prior_art_search(patent_file, search_scope):
    """Basic prior art search fallback for local development."""
    return {
        'patent_file': patent_file.filename,
        'search_scope': search_scope,
        'prior_art_found': [],
        'search_summary': 'Basic search - AI services not available',
        'confidence': 0.3
    }


def _basic_claim_construction(patent_file):
    """Basic claim construction fallback for local development."""
    return {
        'patent_file': patent_file.filename,
        'claim_elements': [],
        'construction_summary': 'Basic construction - AI services not available',
        'confidence': 0.3
    }


def _process_ai_patent_results(search_result, qa_result, analysis_type):
    """Process AI patent analysis results."""
    return {
        'analysis_type': analysis_type,
        'similar_patents': search_result.get('results', [])[:5] if search_result else [],
        'ai_insights': qa_result.get('answer', '') if qa_result else '',
        'confidence': qa_result.get('confidence', 0.5) if qa_result else 0.5
    }


def _process_ai_prior_art_results(prior_art_result, patent_file):
    """Process AI prior art search results."""
    return {
        'patent_file': patent_file.filename,
        'prior_art_documents': prior_art_result.get('results', []) if prior_art_result else [],
        'relevance_scores': [],
        'confidence': 0.7
    }


def _process_ai_claim_construction_results(qa_result, patent_file):
    """Process AI claim construction results."""
    return {
        'patent_file': patent_file.filename,
        'claim_analysis': qa_result.get('answer', '') if qa_result else '',
        'construction_elements': [],
        'confidence': qa_result.get('confidence', 0.5) if qa_result else 0.5
    }
