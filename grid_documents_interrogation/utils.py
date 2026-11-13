import os
import logging
logger = logging.getLogger(__name__)
import json
import tiktoken  # for OpenAI token estimation
import pandas as pd
from urllib.parse import unquote
from typing import List, Tuple, Optional

from langchain_ollama import ChatOllama
from openai import OpenAI

# Django models (for cached text lookup when using file_id)
from core.models import File

from langchain_core.output_parsers import StrOutputParser

# Registry of model context limits (tokens)
MODEL_CONTEXT_WINDOWS = {
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4": 128000,
    "gpt-3.5-turbo": 16000,
    "claude-2": 100000,
    "claude-3-opus": 200000,
    "llama2": 4096,
    "mistral": 8192,
    "minilm": 512,
    "default": 4096,
    # vLLM-hosted models (typical context windows)
    "Qwen/Qwen2.5-7B-Instruct": 32768,
    "mistralai/Mistral-7B-Instruct-v0.3": 32768,
    "meta-llama/Meta-Llama-3.1-8B-Instruct": 128000,
    "microsoft/Phi-3-mini-4k-instruct": 4096,
}


# ───────────────────────── Token helpers ─────────────────────────

def get_token_limit(model: str) -> int:
    return MODEL_CONTEXT_WINDOWS.get(model, MODEL_CONTEXT_WINDOWS["default"])


def _get_encoder(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def estimate_token_count(text: str, model: str = "gpt-3.5-turbo") -> int:
    enc = _get_encoder(model)
    return len(enc.encode(text or ""))


def _split_text_by_tokens(text: str, model_name: str, chunk_size_tokens: int) -> List[str]:
    """
    Chunk text by approximate token count using tiktoken.
    This is more accurate than splitting by words.
    """
    if not text:
        return []
    chunks = []
    try:
        enc = _get_encoder(model_name)
        tokens = enc.encode(text)
        for i in range(0, len(tokens), chunk_size_tokens):
            piece = enc.decode(tokens[i:i + chunk_size_tokens])
            if isinstance(piece, str) and piece:
                chunks.append(piece)
    except Exception as exc:
        logger.warning("Token chunking unavailable (%s); splitting by characters", type(exc).__name__)
        chunks = []
    if not chunks:
        # ~4 characters per token: never silently drop the document.
        size = max(chunk_size_tokens * 4, 4000)
        chunks = [text[i:i + size] for i in range(0, len(text), size)]
    return chunks


def chunk_text_by_token_limit(text: str, model_name: str, max_reserved_tokens: int = 1000) -> List[str]:
    token_limit = get_token_limit(model_name)
    chunk_size = max(token_limit - max_reserved_tokens, 1024)  # keep a reasonable floor
    return _split_text_by_tokens(text, model_name, chunk_size)


def _trim_messages_to_budget(previous_messages: Optional[List[dict]], model: str, budget_tokens: int) -> List[dict]:
    """
    Keep only as many tail messages as fit within budget_tokens.
    Each message is expected to have {'role': 'user'|'assistant', 'content': '...'}.
    """
    if not previous_messages:
        return []

    enc = _get_encoder(model)
    trimmed: List[dict] = []
    running = 0

    # iterate from the end (most recent first), then reverse back
    for msg in reversed(previous_messages):
        content = msg.get("content") or ""
        tokens = len(enc.encode(content))
        if running + tokens > budget_tokens:
            break
        trimmed.append(msg)
        running += tokens

    return list(reversed(trimmed))


# ───────────────────────── Cache helpers ─────────────────────────

def _candidate_output_paths(file_path: str) -> List[str]:
    # Prefer "<original>.txt", fall back to "<root>.txt"
    root, _ = os.path.splitext(file_path)
    return [f"{file_path}.txt", f"{root}.txt"]


def _read_if_exists(path: Optional[str]) -> Optional[str]:
    if path and os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            return None
    return None


def _ensure_parent(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _write_text(path: str, text: str):
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")


def _get_cached_text_for_file(file_obj: File) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (text, used_path). Tries:
      1) storage.output_storage_location
      2) <filepath>.txt
      3) <root>.txt
    """
    # 1) storage.output_storage_location
    storage = getattr(file_obj, "storage", None)
    out_path = getattr(storage, "output_storage_location", None)
    text = _read_if_exists(out_path)
    if text:
        return text, out_path

    # 2) / 3) neighbor text files
    for cand in _candidate_output_paths(file_obj.filepath):
        text = _read_if_exists(cand)
        if text:
            return text, cand

    return None, out_path or _candidate_output_paths(file_obj.filepath)[-1]


def _save_cache_and_update_storage(file_obj: File, text: str, chosen_path: Optional[str]):
    """
    Writes text to chosen_path (if provided) and updates Storage.output_storage_location if needed.
    """
    if not chosen_path:
        # default to "<root>.txt"
        chosen_path = _candidate_output_paths(file_obj.filepath)[-1]
    try:
        _write_text(chosen_path, text)
        # Persist on Storage if field exists
        storage = getattr(file_obj, "storage", None)
        if storage and getattr(storage, "output_storage_location", None) != chosen_path:
            storage.output_storage_location = chosen_path
            storage.save(update_fields=["output_storage_location"])
    except Exception as e:
        print(f"[WARN] Failed to persist cache for file_id={file_obj.id} at {chosen_path}: {e}")


# ───────────────────────── LLM dispatch ─────────────────────────

def _build_messages_for_openai(previous_messages: Optional[List[dict]], chunk: str, query_text: str, model: str) -> List[dict]:
    """
    Construct OpenAI messages while respecting a token budget.
    We keep system + (trimmed history) + current chunk + user query.
    """
    system_prompt = "You are a precise, concise data analysis and document QA assistant."
    token_limit = get_token_limit(model)

    # Reserve space for system + current prompt + output
    reserve_for_output = 2048
    reserve_for_chunk_and_question = estimate_token_count(chunk, model) + estimate_token_count(query_text, model) + 200
    budget_for_history = max(token_limit - reserve_for_output - reserve_for_chunk_and_question, 512)

    trimmed_history = _trim_messages_to_budget(previous_messages, model, budget_for_history)

    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(trimmed_history)
    messages.append({
        "role": "user",
        "content": f"Document excerpt:\n\n{chunk}\n\nQuestion: {query_text}\n\n"
                   f"Answer briefly, cite exact facts you used, and say if the answer is uncertain."
    })
    return messages



def _history_to_text(previous_messages: Optional[List[dict]], limit: int = 10) -> str:
    """
    Collapse your {'role','content'} history into a compact plain-text prefix.
    Keeps the last `limit` messages (user/assistant).
    """
    if not previous_messages:
        return ""
    lines: List[str] = []
    for msg in previous_messages[-limit:]:
        role = (msg.get("role") or "user").strip().lower()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role not in ("user", "assistant", "system"):
            role = "user"
        lines.append(f"{role.capitalize()}: {content}")
    return "\n".join(lines)


def _chat_completion(client, model, messages, temperature, timeout, max_out=1024):
    """
    One chat completion that copes with model generations: newer OpenAI models want
    `max_completion_tokens` and may reject a custom temperature, older servers only
    know `max_tokens`. Retries once with the parameters the server asked for.
    """
    kwargs = {"model": model, "messages": messages, "max_tokens": max_out, "temperature": temperature,
              "timeout": timeout}
    for _ in range(3):
        try:
            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as exc:
            text = str(exc).lower()
            changed = False
            if "max_tokens" in text and "max_completion_tokens" in text and "max_tokens" in kwargs:
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                changed = True
            if "temperature" in text and ("unsupported" in text or "does not support" in text) and "temperature" in kwargs:
                kwargs.pop("temperature")
                changed = True
            if not changed:
                raise
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def dispatch_to_llm(
    query_text: str,
    chunk: str,
    llm_config: dict,
    previous_messages: Optional[List[dict]] = None
) -> str:
    """
    Provider-agnostic dispatcher that returns a STRING response.
    Supported providers: 'openai', 'ollama'

    llm_config:
      - provider: 'openai' | 'ollama'
      - model: model name (required for both)
      - api_key: OpenAI key (openai only)
      - endpoint: base URL (ollama only), e.g. 'http://ollama:11434'
      - temperature: float (optional)
      - timeout: int seconds (optional; openai handled by client, ollama handled by LC)
      - num_ctx: int context tokens (ollama only, optional)
    """
    provider = (llm_config.get("provider") or "openai").lower()
    model = llm_config.get("model") or "gpt-3.5-turbo"
    temperature = float(llm_config.get("temperature", 0.2))
    timeout = int(llm_config.get("timeout", 120))

    if provider == "openai":
        api_key = llm_config.get("api_key")
        client = OpenAI(api_key=api_key) if api_key else OpenAI()

        # Reuse your existing helper to respect token budgets
        messages = _build_messages_for_openai(previous_messages, chunk, query_text, model)
        try:
            text = _chat_completion(client, model, messages, temperature, timeout)
            return text if isinstance(text, str) else str(text)
        except Exception as e:
            raise RuntimeError(f"OpenAI invocation failed (model={model}): {e}")

    elif provider == "ollama":
        # Choose endpoint: llm_config -> env -> default docker service name
        base_url = (
            llm_config.get("endpoint")
            or os.getenv("OLLAMA_URL")
            or "http://ollama:11434"
        )

        # LangChain's ChatOllama .invoke() returns an AIMessage; use .predict() to get str
        # num_ctx is optional and only applied if provided
        chat_kwargs = {
            "model": model,
            "base_url": base_url,
            "temperature": temperature,
        }
        if "num_ctx" in llm_config:
            chat_kwargs["num_ctx"] = int(llm_config["num_ctx"])

        try:
            chat = ChatOllama(**chat_kwargs)

            history_text = _history_to_text(previous_messages, limit=10)
            # A compact, consistent prompt that works for both small & larger models
            prompt_parts = [
                "You are a precise, concise data/document QA assistant.",
                ("Conversation so far:\n" + history_text) if history_text else None,
                "Document:\n" + (chunk or "")[:200000],  # guard against huge chunk strings
                "Task:\nAnswer the user question using only the document facts. "
                "If you are unsure, say so.",
                "Question:\n" + (query_text or ""),
                "Answer:"
            ]
            prompt = "\n\n".join([p for p in prompt_parts if p])

            # ChatOllama doesn't expose a per-call timeout; rely on upstream task timeouts
            # text = chat.predict(prompt)
            text = (chat | StrOutputParser()).invoke(prompt)
            return text if isinstance(text, str) else str(text)

        except Exception as e:
            # Include url & model to make ops/debugging trivial
            raise RuntimeError(f"Ollama invocation failed (url={base_url}, model={model}): {e}")

    elif provider == "litellm":
        # LiteLLM proxy: one OpenAI-compatible endpoint in front of every provider
        from core.llm_providers import resolve_endpoint, resolve_api_key, openai_compatible_base

        base_url = openai_compatible_base("litellm", resolve_endpoint("litellm", llm_config.get("endpoint")))
        api_key = resolve_api_key("litellm", llm_config.get("api_key")) or "EMPTY"
        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            messages = _build_messages_for_openai(previous_messages, chunk, query_text, model)
            text = _chat_completion(client, model, messages, temperature, timeout)
            return text if isinstance(text, str) else str(text)
        except Exception as e:
            raise RuntimeError(f"LiteLLM invocation failed (model={model}): {e}")

    elif provider == "vllm":
        # vLLM exposes an OpenAI-compatible API
        base_url = (
            llm_config.get("endpoint")
            or os.getenv("VLLM_URL")
            or "http://vllm:8000/v1"
        )
        api_key = llm_config.get("api_key") or os.getenv("VLLM_API_KEY", "EMPTY")

        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            messages = _build_messages_for_openai(previous_messages, chunk, query_text, model)
            text = _chat_completion(client, model, messages, temperature, timeout)
            return text if isinstance(text, str) else str(text)
        except Exception as e:
            raise RuntimeError(f"vLLM invocation failed (url={base_url}, model={model}): {e}")

    elif provider == "langchain":
        raise NotImplementedError("LangChain provider wrapper is not implemented here.")

    elif provider == "mcp":
        raise NotImplementedError("MCP provider not yet integrated.")

    else:
        raise ValueError(f"Unsupported provider: {provider}")






'''
def dispatch_to_llm(query_text: str, chunk: str, llm_config: dict, previous_messages: Optional[List[dict]] = None):
    provider = llm_config.get("provider", "openai")
    model = llm_config.get("model", "gpt-3.5-turbo")
    api_key = llm_config.get("api_key")
    endpoint = llm_config.get("endpoint")  # used for ollama if provided

    if provider == "openai":
        client = OpenAI(api_key=api_key) if api_key else OpenAI()
        messages = _build_messages_for_openai(previous_messages, chunk, query_text, model)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=1024,
            temperature=0.2,
        )
        return response.choices[0].message.content
        curl -L https://iterm2.com/shell_integration/install_shell_integration_and_utilities.sh | bash


    elif provider == "ollama":
        try:
            base_url = endpoint or "http://ollama:11434"
            chat = ChatOllama(model=model, base_url=base_url)
            # Simple condensed context; you can mirror OpenAI structure if desired
            context = ""
            if previous_messages:
                for msg in previous_messages[-10:]:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    context += f"{role.capitalize()}: {content}\n"
            context += f"\nDocument:\n{chunk}\n\nQuestion: {query_text}"
            return chat.invoke(context)
        except Exception as e:
            raise RuntimeError(f"Ollama invocation failed: {e}")

    elif provider == "langchain":
        raise NotImplementedError("LangChain support is not yet implemented.")

    elif provider == "mcp":
        raise NotImplementedError("MCP provider not yet integrated.")

    else:
        raise ValueError(f"Unsupported provider: {provider}")
'''

# ───────────────────────── File path / id entrypoints ─────────────────────────

def _materialize_text_from_file_path(file_path: str) -> str:
    """
    Your original path-based extraction (one-off). Used for backward-compat.
    """
    from .file_readers import read_file, convert_dataframe_to_text
    content, is_tabular = read_file(file_path)
    if content is None:
        raise ValueError("File content could not be read. Ensure the file format is supported.")
    if is_tabular:
        content = convert_dataframe_to_text(content)
    if not isinstance(content, str):
        content = str(content)
    if not content.strip():
        raise ValueError("The file appears empty or unreadable.")
    return content


def _get_or_build_cached_text_for_file_id(file_id: int) -> str:
    """
    New, efficient path: use cached text if present; otherwise extract and store.
    """
    file_obj = File.objects.select_related("storage").get(id=file_id)

    # 1) try cache
    cached_text, chosen_path = _get_cached_text_for_file(file_obj)
    if cached_text:
        return cached_text

    # 2) fallback extraction once, then cache
    try:
        text = _materialize_text_from_file_path(file_obj.filepath)
    except ValueError:
        # 3) the text the platform extracted when the document was indexed
        if file_obj.content and file_obj.content.strip():
            return file_obj.content
        raise
    _save_cache_and_update_storage(file_obj, text, chosen_path)
    return text


def execute_file_query(
    query_text: str,
    file_path: Optional[str],
    llm_config: dict,
    previous_messages: Optional[List[dict]] = None,
    *,
    file_id: Optional[int] = None
) -> str:
    """
    Backward-compatible entrypoint:
      - If file_id is provided, use cached-text pipeline (fast).
      - Else if file_path is provided, do the legacy direct-read pipeline.
    """
    if file_id is not None:
        content = _get_or_build_cached_text_for_file_id(file_id)
    elif file_path:
        # Legacy behavior (no caching)
        content = _materialize_text_from_file_path(file_path)
    else:
        raise ValueError("Either file_id or file_path must be supplied.")

    model_name = llm_config.get("model", "default")
    token_chunks = chunk_text_by_token_limit(content, model_name, max_reserved_tokens=1500)

    answers = []
    for i, chunk in enumerate(token_chunks):
        try:
            print(f"[INFO] Sending chunk {i+1}/{len(token_chunks)} to LLM...")
            answer = dispatch_to_llm(query_text, chunk, llm_config, previous_messages=previous_messages)
            answers.append(answer)
        except Exception as e:
            print(f"[ERROR] Failed to process chunk {i+1}: {e}")
            answers.append(f"[Error processing chunk: {e}]")
    return "\n\n---\n\n".join(answers)


# ───────────────────────── DB entrypoint ─────────────────────────

def execute_db_query(
    query_text: str,
    connection_string: str,
    table_name: str,
    llm_config: dict,
    stream: bool = False,
    chunk_size: int = 100,
    previous_messages: Optional[List[dict]] = None
):
    from .db_query_tools import fetch_column_names, generate_sql_query, execute_sql_query, fallback_select
    from .safety import UnsafeTarget, checked_url, describe

    try:
        url = checked_url(connection_string)
        logger.info("DocuChat DB query on %s table=%s", describe(url), table_name)
        column_names = fetch_column_names(url, table_name)
        sql_query = generate_sql_query(table_name, query_text, column_names, llm_config) or fallback_select(url, table_name)
        result = execute_sql_query(url, sql_query, stream=stream, chunk_size=chunk_size)

        # Normalize results to text for LLM
        text_data = ""
        if isinstance(result, pd.DataFrame):
            print(f"[DEBUG] Got DataFrame with shape: {result.shape}")
            text_data = result.to_csv(index=False)
        elif hasattr(result, '__iter__') and not isinstance(result, (str, bytes, dict)):
            chunks = []
            for item in result:
                if isinstance(item, pd.DataFrame):
                    if not item.empty:
                        chunks.append(item.to_csv(index=False))
                else:
                    chunks.append(str(item))
            text_data = "\n\n".join(chunks)
        else:
            text_data = str(result)

        print(f"[DEBUG] Extracted text data length: {len(text_data)}")
        if not text_data.strip():
            print("[WARN] Query returned no usable data.")
            return "The table exists but contains no data rows to analyze."

        # Token-chunk the data for LLM, reserve room for answer
        model_name = llm_config.get("model", "default")
        token_chunks = chunk_text_by_token_limit(text_data, model_name, max_reserved_tokens=1500)

        answers = []
        for i, chunk in enumerate(token_chunks):
            try:
                print(f"[INFO] Sending chunk {i+1}/{len(token_chunks)} to LLM...")
                answer = dispatch_to_llm(query_text, chunk, llm_config, previous_messages=previous_messages)
                answers.append(answer)
            except Exception as e:
                print(f"[ERROR] Failed to process chunk {i+1}: {e}")
                answers.append(f"[Error processing chunk: {e}]")

        return "\n\n---\n\n".join(answers)

    except UnsafeTarget as e:
        return f"[ERROR] {e}"
    except Exception as e:
        logger.warning("DocuChat DB query failed: %s", type(e).__name__)
        return "[ERROR] The database query failed. Check the connection details and table name."

