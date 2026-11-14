"""Run through manage.py shell in the isolated localhost stack."""
import os
assert os.environ.get('LOCAL_AUDIT', '').lower() == 'true'
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')
    assert cursor.fetchone()[0]
    cursor.execute("SELECT rolpassword LIKE 'SCRAM-SHA-256$%' FROM pg_authid WHERE rolname=current_user")
    assert cursor.fetchone()[0]
print('PASS PostgreSQL connection uses TLS and this local role has a SCRAM password')
import dspy
assert dspy.cache.enable_disk_cache is False
assert dspy.cache.enable_memory_cache is False
print('PASS DSPy disk and memory caches disabled')
import en_core_web_lg
from azure.ai.translation.document.models import DocumentTranslationInput, TranslationTarget
from unstructured.partition.pdf import partition_pdf
from sentence_transformers import SentenceTransformer
print('PASS real spaCy model, Azure SDK 2 models, PDF parser and embedding libraries import')
