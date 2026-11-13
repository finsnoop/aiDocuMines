import gc
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from .utils import execute_file_query, execute_db_query


@shared_task(soft_time_limit=600)
def process_file_query(
    query_text,
    file_path=None,
    llm_config: dict = None,
    previous_messages=None,
    file_id: int = None,
):
    """
    Processes file-based LLM query with optional memory context.

    Backward compatible:
      - old callers pass (query_text, file_path, llm_config, previous_messages)
      - new callers pass (query_text, llm_config=..., previous_messages=..., file_id=123)

    Prefer passing file_id for cached, fast path.
    """
    try:
        llm_cfg = llm_config or {}
        result = execute_file_query(
            query_text=query_text,
            file_path=file_path,
            llm_config=llm_cfg,
            previous_messages=previous_messages,
            file_id=file_id,  # new fast path
        )
        gc.collect()
        return result
    except SoftTimeLimitExceeded:
        return 'Task exceeded soft time limit'
    except Exception as e:
        return f'Error during file query: {str(e)}'


@shared_task(soft_time_limit=300)
def process_db_query(query_text, connection_string, table_name, llm_config: dict, stream=False, previous_messages=None):
    """
    Processes DB-based LLM query with optional memory context.
    """
    try:
        result = execute_db_query(
            query_text,
            connection_string,
            table_name,
            llm_config,
            stream=stream,
            previous_messages=previous_messages
        )
        gc.collect()
        if stream and hasattr(result, '__iter__'):
            return "\n\n".join([df.to_csv(index=False) for df in result])
        return result.to_csv(index=False) if hasattr(result, 'to_csv') else result
    except SoftTimeLimitExceeded:
        return 'Task exceeded soft time limit'
    except Exception as e:
        return f'Error during DB query: {str(e)}'

