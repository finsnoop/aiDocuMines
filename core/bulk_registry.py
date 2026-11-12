from typing import Dict, List, Optional, Callable, Any
import logging

logger = logging.getLogger(__name__)


def _bulk_call_adapter(task_func, *, single_file_param=True, user_id_param=True,
                       file_param_name='file_id', run_creator=None, extra_params=None):
    """
    Build a call adapter that translates (file_id, user_id, **kwargs)
    into the correct task_func.delay(...) signature.

    - single_file_param: if True, task_func expects a single file_id;
      if False, task_func expects a list (file_ids) and we wrap in [file_id].
    - user_id_param: if True, pass user_id to task_func.
    - file_param_name: name of the file parameter (default: 'file_id').
    - run_creator: callable(file_id, user_id) -> dict of extra kw args for the task.
    - extra_params: dict of additional static kwargs to pass.
    
    When called with multi_file=True, file_ids is passed directly as the file_ids param.
    """
    def adapter(file_id_or_ids, user_id, **kwargs):
        multi_file = kwargs.pop('multi_file', False)
        inline = kwargs.pop('inline', False)
        args = {}
        if multi_file:
            args['file_ids'] = file_id_or_ids
            if run_creator and file_id_or_ids:
                args.update(run_creator(file_id_or_ids[0], user_id))
        elif single_file_param:
            args[file_param_name] = file_id_or_ids
        else:
            args['file_ids'] = [file_id_or_ids]
        if user_id_param:
            args['user_id'] = user_id
        if run_creator and not multi_file:
            args.update(run_creator(file_id_or_ids, user_id))
        if extra_params:
            args.update(extra_params)
        # inline=True runs the service in the calling worker and returns its
        # result, so bulk jobs report real completion and real errors.
        return task_func.apply(kwargs=args) if inline else task_func.delay(**args)

    adapter.__name__ = f'{task_func.__name__}_bulk_adapter'
    return adapter


class BulkServiceHandler:
    def __init__(self, job_type: str, name: str, vertical: str,
                 task_func: Callable,
                 requires_run: bool = True,
                 run_model: Optional[Any] = None,
                 call_adapter: Optional[Callable] = None,
                 batch_all: bool = False):
        self.job_type = job_type
        self.name = name
        self.vertical = vertical
        self.task_func = task_func
        self.requires_run = requires_run
        self.run_model = run_model
        self._call_adapter = call_adapter
        self.batch_all = batch_all

    def call(self, file_id: int, user_id: int, inline: bool = False, **kwargs):
        if self._call_adapter:
            return self._call_adapter(file_id, user_id, inline=inline, **kwargs)
        args = (file_id, user_id) if self.requires_run else (file_id,)
        return self.task_func.apply(args=args) if inline else self.task_func.delay(*args)

    def call_batch(self, file_ids: List[int], user_id: int, inline: bool = False, **kwargs):
        """Dispatch a single task with all file_ids for batch_all handlers."""
        if self._call_adapter:
            return self._call_adapter(file_ids, user_id, multi_file=True, inline=inline, **kwargs)
        return self.task_func.apply(args=(file_ids, user_id)) if inline else self.task_func.delay(file_ids, user_id)


_registry: Dict[str, BulkServiceHandler] = {}


def register_bulk_handler(job_type: str, handler: BulkServiceHandler):
    _registry[job_type] = handler
    logger.info(f"Registered bulk handler: {job_type} -> {handler.name}")


def get_bulk_handler(job_type: str) -> Optional[BulkServiceHandler]:
    return _registry.get(job_type)


def list_bulk_handlers() -> List[Dict]:
    return [
        {
            "job_type": h.job_type,
            "name": h.name,
            "vertical": h.vertical,
        }
        for h in _registry.values()
    ]



def _requesting_user(user_id):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(id=user_id).select_related("client").first()


def _tenant_fields(user_id):
    """Owner + tenant for processing runs, so every bulk run is attributable to a tenant."""
    from core.access import run_owner_fields
    user = _requesting_user(user_id)
    return run_owner_fields(user) if user else {}

# ---- Run creators ----

def _create_run(file_id, user_id, model_cls, status_default='Pending', user_field='user',
                id_field='id', extra_fields=None):
    """Create a run object and return a dict of field->value for the task call."""
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()

    fields = {}
    if hasattr(model_cls, 'status'):
        fields['status'] = status_default
    if hasattr(model_cls, user_field) and owner:
        fields[user_field] = owner
    if extra_fields:
        fields.update(extra_fields)

    run_obj = model_cls.objects.create(**fields)
    return {id_field: run_obj.id}


def _run_ocr(file_id, user_id):
    from document_ocr.models import OCRRun
    run = OCRRun.objects.create(status='Pending', **_tenant_fields(user_id))
    return {'run_id': run.id}


def _run_anonymization(file_id, user_id):
    from document_anonymizer.models import AnonymizationRun
    run = AnonymizationRun.objects.create(status='Pending', **_tenant_fields(user_id))
    return {'run_id': run.id}


def _run_translation(file_id, user_id):
    from document_translation.models import TranslationRun
    run = TranslationRun.objects.create(status='Pending', **_tenant_fields(user_id))
    return {'translation_run_id': run.id}


def _run_pe(file_id, user_id):
    from private_equity.models import DueDiligenceRun
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    client = None
    if user:
        client = getattr(user, 'client', None)
    run = DueDiligenceRun.objects.create(
        client=client,
        run_name=f'Bulk PE {file_id}',
    )
    return {'dd_run_id': run.id}


def _run_ca(file_id, user_id):
    from class_actions.models import MassClaimsRun
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    client = None
    if user:
        client = getattr(user, 'client', None)
    run = MassClaimsRun.objects.create(
        client=client,
        status='intake',
    )
    return {'mc_run_id': run.id}


def _run_le(file_id, user_id):
    from labor_employment.models import WorkplaceCommunicationsRun
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    client = None
    if user:
        client = getattr(user, 'client', None)
    run = WorkplaceCommunicationsRun.objects.create(
        client=client,
    )
    return {'comm_run_id': run.id}


def _run_patent(file_id, user_id):
    from ip_litigation.models import PatentDocument
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    doc = PatentDocument.objects.create(
        user=owner,
        status='pending',
    )
    return {'patent_document_id': doc.id}


def _run_prior_art(file_id, user_id):
    from ip_litigation.models import PriorArtDocument
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    doc = PriorArtDocument.objects.create(
        user=owner,
    )
    return {'prior_art_document_id': doc.id}


def _run_infringement(file_id, user_id):
    from ip_litigation.models import InfringementAnalysis
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    analysis = InfringementAnalysis.objects.create(
        user=owner,
    )
    return {'infringement_analysis_id': analysis.id}


def _run_structure(file_id, user_id):
    from document_structures.models import DocumentStructureRun
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    run = DocumentStructureRun.objects.create(
        user=owner,
        status='Pending',
    )
    return {'document_structure_run_id': run.id}


def _run_clustering(file_id, user_id):
    from document_classification.models import ClusteringRun
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    run = ClusteringRun.objects.create(
        user=owner,
        status='Pending',
    )
    return {'run_id': run.id}


def _run_redlining(file_id, user_id):
    from document_redlining.models import RedliningRun
    user = _requesting_user(user_id)
    run = RedliningRun.objects.create(
        status='Pending',
        client_name=(user.username or user.email) if user else '',
    )
    return {'run_id': run.id, 'comparison_file_id': file_id}


def _run_core_run(file_id, user_id):
    from core.models import Run
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    run = Run.objects.create(
        user=owner,
        status='Uploaded',
    )
    return {'run_id': run.run_id}


def _run_dsar(file_id, user_id):
    from regulatory_compliance.models import DSARRequest
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    req = DSARRequest.objects.create(
        user=owner,
        status='received',
    )
    return {'dsar_request_id': req.id}


def _run_redaction(file_id, user_id):
    from regulatory_compliance.models import RedactionTask
    from django.contrib.auth import get_user_model
    from core.models import File
    try:
        file_obj = File.objects.get(id=file_id)
        owner = file_obj.user
    except File.DoesNotExist:
        User = get_user_model()
        owner = User.objects.filter(id=user_id).first()
    task = RedactionTask.objects.create(
        user=owner,
        status='pending',
    )
    return {'redaction_task_id': task.id}


# ---- Handler registration ----

def discover_and_register():
    try:
        from document_anonymizer.tasks import anonymize_document_task
        register_bulk_handler("ai_anonymization", BulkServiceHandler(
            job_type="ai_anonymization",
            name="Document Anonymization",
            vertical="AI Document Processing",
            task_func=anonymize_document_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                anonymize_document_task,
                single_file_param=True,
                user_id_param=False,
                file_param_name='file_id',
                run_creator=_run_anonymization,
                extra_params={'file_type': 'plain'},
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_anonymization: {e}")

    try:
        from document_ocr.tasks import process_ocr
        register_bulk_handler("ai_ocr", BulkServiceHandler(
            job_type="ai_ocr",
            name="OCR Text Extraction",
            vertical="AI Document Processing",
            task_func=process_ocr,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                process_ocr,
                single_file_param=True,
                user_id_param=False,
                file_param_name='file_id',
                run_creator=_run_ocr,
                extra_params={'ocr_option': 'basic'},
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_ocr: {e}")

    try:
        from document_translation.tasks import translate_document_task
        register_bulk_handler("ai_translation", BulkServiceHandler(
            job_type="ai_translation",
            name="Document Translation",
            vertical="AI Document Processing",
            task_func=translate_document_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                translate_document_task,
                single_file_param=True,
                user_id_param=False,
                file_param_name='file_id',
                run_creator=_run_translation,
                extra_params={'from_language': 'auto', 'to_language': 'en'},
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_translation: {e}")

    try:
        from document_search.tasks import index_file
        register_bulk_handler("ai_document_search", BulkServiceHandler(
            job_type="ai_document_search",
            name="Semantic Document Search Indexing",
            vertical="AI Document Processing",
            task_func=index_file,
            requires_run=False,
            call_adapter=_bulk_call_adapter(
                index_file,
                single_file_param=True,
                user_id_param=False,
                file_param_name='file_id',
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_document_search: {e}")

    try:
        from private_equity.tasks import classify_document_task, extract_risk_clauses_task
        register_bulk_handler("pe_classify", BulkServiceHandler(
            job_type="pe_classify",
            name="Classify Documents",
            vertical="Private Equity",
            task_func=classify_document_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                classify_document_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='file_id',
                run_creator=_run_pe,
            ),
        ))
        register_bulk_handler("pe_extract_risk_clauses", BulkServiceHandler(
            job_type="pe_extract_risk_clauses",
            name="Extract Risk Clauses",
            vertical="Private Equity",
            task_func=extract_risk_clauses_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                extract_risk_clauses_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='file_id',
                run_creator=_run_pe,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping pe handlers: {e}")

    try:
        from class_actions.tasks import cull_evidence_documents_task, redact_pii_task
        register_bulk_handler("ca_evidence_culling", BulkServiceHandler(
            job_type="ca_evidence_culling",
            name="Evidence Culling",
            vertical="Class Actions",
            task_func=cull_evidence_documents_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                cull_evidence_documents_task,
                single_file_param=False,
                user_id_param=True,
                run_creator=_run_ca,
            ),
        ))
        register_bulk_handler("ca_pii_redaction", BulkServiceHandler(
            job_type="ca_pii_redaction",
            name="PII Redaction",
            vertical="Class Actions",
            task_func=redact_pii_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                redact_pii_task,
                single_file_param=False,
                user_id_param=True,
                run_creator=_run_ca,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ca handlers: {e}")

    try:
        from labor_employment.tasks import analyze_communications_task
        register_bulk_handler("le_analyze_communications", BulkServiceHandler(
            job_type="le_analyze_communications",
            name="Analyze Communications",
            vertical="Labor & Employment",
            task_func=analyze_communications_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                analyze_communications_task,
                single_file_param=False,
                user_id_param=True,
                run_creator=_run_le,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping le_analyze_communications: {e}")

    try:
        from labor_employment.tasks import analyze_wage_hour_task
        register_bulk_handler("le_wage_hour_analysis", BulkServiceHandler(
            job_type="le_wage_hour_analysis",
            name="Wage & Hour Analysis",
            vertical="Labor & Employment",
            task_func=analyze_wage_hour_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                analyze_wage_hour_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='employee_list',
                run_creator=_run_le,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping le_wage_hour_analysis: {e}")

    try:
        from labor_employment.tasks import compare_policies_task
        register_bulk_handler("le_policy_comparison", BulkServiceHandler(
            job_type="le_policy_comparison",
            name="Policy Comparison",
            vertical="Labor & Employment",
            task_func=compare_policies_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                compare_policies_task,
                single_file_param=False,
                user_id_param=True,
                file_param_name='policy_file_ids',
                run_creator=_run_le,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping le_policy_comparison: {e}")

    try:
        from ip_litigation.tasks import extract_patent_data_task
        register_bulk_handler("ip_analyze_patent", BulkServiceHandler(
            job_type="ip_analyze_patent",
            name="Patent Analysis",
            vertical="IP Litigation",
            task_func=extract_patent_data_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                extract_patent_data_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='patent_document_id',
                run_creator=_run_patent,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ip_analyze_patent: {e}")

    try:
        from ip_litigation.tasks import search_prior_art_task
        register_bulk_handler("ip_prior_art_search", BulkServiceHandler(
            job_type="ip_prior_art_search",
            name="Prior Art Search",
            vertical="IP Litigation",
            task_func=search_prior_art_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                search_prior_art_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='prior_art_document_id',
                run_creator=_run_prior_art,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ip_prior_art_search: {e}")

    try:
        from ip_litigation.tasks import analyze_infringement_task
        register_bulk_handler("ip_infringement", BulkServiceHandler(
            job_type="ip_infringement",
            name="Infringement Analysis",
            vertical="IP Litigation",
            task_func=analyze_infringement_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                analyze_infringement_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='infringement_analysis_id',
                run_creator=_run_infringement,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ip_infringement: {e}")

    try:
        from document_structures.tasks import run_document_partition_task as extract_document_structure_task
        register_bulk_handler("ai_document_structure", BulkServiceHandler(
            job_type="ai_document_structure",
            name="Document Structure Analysis",
            vertical="AI Document Processing",
            task_func=extract_document_structure_task,
            requires_run=False,
            call_adapter=_bulk_call_adapter(
                extract_document_structure_task,
                single_file_param=True,
                user_id_param=False,
                file_param_name='document_structure_run_id',
                run_creator=_run_structure,
                extra_params={'store_embeddings': True},
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_document_structure: {e}")

    try:
        from document_classification.tasks import cluster_documents_task
        register_bulk_handler("ai_document_classification", BulkServiceHandler(
            job_type="ai_document_classification",
            name="Document Classification",
            vertical="AI Document Processing",
            task_func=cluster_documents_task,
            requires_run=False,
            batch_all=True,
            call_adapter=_bulk_call_adapter(
                cluster_documents_task,
                single_file_param=False,
                user_id_param=False,
                run_creator=_run_clustering,
                extra_params={'clustering_method': 'agglomerative'},
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_document_classification: {e}")

    try:
        from document_redlining.tasks import perform_redlining_task as redline_document_task
        register_bulk_handler("ai_redlining", BulkServiceHandler(
            job_type="ai_redlining",
            name="Document Redlining",
            vertical="AI Document Processing",
            task_func=redline_document_task,
            requires_run=False,
            call_adapter=_bulk_call_adapter(
                redline_document_task,
                single_file_param=True,
                user_id_param=False,
                file_param_name='file_id',
                run_creator=_run_redlining,
                extra_params={'author': 'Bulk Processing'},
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_redlining: {e}")

    try:
        from core.tasks import process_metadata
        register_bulk_handler("ai_metadata_extraction", BulkServiceHandler(
            job_type="ai_metadata_extraction",
            name="Metadata Extraction",
            vertical="AI Document Processing",
            task_func=process_metadata,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                process_metadata,
                single_file_param=True,
                user_id_param=False,
                file_param_name='file_id',
                run_creator=_run_core_run,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping ai_metadata_extraction: {e}")

    try:
        from regulatory_compliance.tasks import process_dsar_request_task
        register_bulk_handler("rc_dsar_processing", BulkServiceHandler(
            job_type="rc_dsar_processing",
            name="DSAR Processing",
            vertical="Regulatory Compliance",
            task_func=process_dsar_request_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                process_dsar_request_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='dsar_request_id',
                run_creator=_run_dsar,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping rc_dsar_processing: {e}")

    try:
        from regulatory_compliance.tasks import perform_document_redaction_task
        register_bulk_handler("rc_redaction", BulkServiceHandler(
            job_type="rc_redaction",
            name="Document Redaction",
            vertical="Regulatory Compliance",
            task_func=perform_document_redaction_task,
            requires_run=True,
            call_adapter=_bulk_call_adapter(
                perform_document_redaction_task,
                single_file_param=True,
                user_id_param=True,
                file_param_name='redaction_task_id',
                run_creator=_run_redaction,
            ),
        ))
    except ImportError as e:
        logger.warning(f"Skipping rc_redaction: {e}")

    logger.info(f"Bulk registry initialized with {len(_registry)} handlers")
