import os
import logging
logger = logging.getLogger(__name__)
import re
import json
import tiktoken  # for OpenAI token estimation
import pandas as pd
from urllib.parse import unquote
from typing import List, Tuple, Optional, Dict

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
    # LiteLLM default — large context for proxy-routed models
    "litellm": 128000,
}


# ───────────────────────── Semantic Chunking ─────────────────────────

def _paragraph_split(text: str) -> List[Dict[str, str]]:
    """
    Split text into paragraphs (semantic units). Uses double newlines as
    primary split, then single newlines for shorter paragraphs.
    Returns list of dicts with 'text' and 'source' (heading if any).
    """
    if not text:
        return []

    # First split on double-newlines (paragraph boundaries)
    raw_blocks = re.split(r'\n\s*\n', text)
    paragraphs: List[Dict[str, str]] = []

    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue

        # Detect headings (lines that are short, all caps, or followed by ---)
        lines = block.split('\n')
        if len(lines) == 1:
            # Single-line block: could be heading or paragraph
            clean = lines[0].strip()
            if re.match(r'^#+\s|^[A-Z][A-Z\s\-/]{3,}$|^\d+\.\s+\w', clean):
                paragraphs.append({'text': clean, 'source': ''})
            else:
                paragraphs.append({'text': clean, 'source': ''})
        else:
            # Multi-line block
            first_line = lines[0].strip()
            if _is_heading(first_line, lines):
                heading = first_line
                body = '\n'.join(lines[1:]).strip()
                paragraphs.append({'text': body, 'source': heading})
            else:
                paragraphs.append({'text': block, 'source': ''})

    return paragraphs


def _is_heading(line: str, all_lines: List[str]) -> bool:
    """Heuristic: detect if a line is a section heading."""
    clean = re.sub(r'^[#*\-\s]+', '', line).strip()
    if not clean:
        return False
    # Markdown-style heading
    if re.match(r'^#{1,6}\s', line):
        return True
    # Short all-caps line followed by longer body
    if len(clean) < 80 and clean.isupper() and len(all_lines) > 1:
        return True
    # Numbered section (e.g., "1. Introduction", "Section 2: ...")
    if re.match(r'^(Section\s*\d+|Article\s*\d+|\d+\.\d*)\s', clean, re.IGNORECASE):
        return True
    return False


def _token_count(text: str, model: str = "cl100k_base") -> int:
    """Estimate token count for a text."""
    try:
        try:
            enc = tiktoken.get_encoding(model)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        n = enc.encode(text or "")
        return len(n) if isinstance(n, (list, tuple)) else max(1, len(text or "") // 4)
    except Exception:
        return max(1, len(text or "") // 4)   # ~4 characters per token when tiktoken is unavailable


def semantic_chunk(
    text: str,
    model_name: str = "gpt-4o-mini",
    target_tokens: int = 3000,
    overlap_tokens: int = 300,
) -> List[Dict[str, str]]:
    """
    Semantic chunking with overlap and provenance links.

    Algorithm:
    1. Split text into paragraphs (semantic units)
    2. Greedily build chunks that respect target_tokens
    3. Include overlap from previous chunk's end
    4. Each chunk gets a header with position info and source section

    Returns list of dicts: {'content': str, 'heading': str, 'index': int, 'total': int}
    """
    paragraphs = _paragraph_split(text)
    if not paragraphs:
        return []

    chunks: List[Dict[str, str]] = []
    current_chunk_lines: List[str] = []
    current_tokens = 0
    current_heading = ''

    for para in paragraphs:
        para_text = para['text']
        para_heading = para.get('source', '')

        # Track heading changes
        if para_heading:
            current_heading = para_heading

        para_tokens = _token_count(para_text, model_name)

        # If this paragraph alone exceeds target, split it further by sentences
        if para_tokens > target_tokens:
            subchunks = _sentence_split_chunk(para_text, model_name, target_tokens, overlap_tokens)
            for sc in subchunks:
                chunks.append({
                    'content': sc,
                    'heading': current_heading,
                    'index': 0,  # patched below
                    'total': 0,
                })
            continue

        # If adding this paragraph would exceed target, flush current chunk
        if current_tokens + para_tokens > target_tokens and current_chunk_lines:
            chunk_text = '\n\n'.join(current_chunk_lines)
            chunks.append({
                'content': chunk_text,
                'heading': current_heading,
                'index': 0,
                'total': 0,
            })

            # Start new chunk with overlap from the end of the previous chunk
            prev_text = chunk_text
            overlap_text = _extract_overlap(prev_text, model_name, overlap_tokens)
            current_chunk_lines = [overlap_text] if overlap_text else []
            current_tokens = _token_count(overlap_text, model_name) if overlap_text else 0

            if para_heading:
                current_heading = para_heading

        current_chunk_lines.append(para_text)
        current_tokens += para_tokens

    # Flush final chunk
    if current_chunk_lines:
        chunk_text = '\n\n'.join(current_chunk_lines)
        chunks.append({
            'content': chunk_text,
            'heading': current_heading,
            'index': 0,
            'total': 0,
        })

    # Set index and total, add provenance headers
    total = len(chunks)
    for i, ch in enumerate(chunks):
        ch['index'] = i + 1
        ch['total'] = total
        ch['content'] = _format_chunk_content(ch, i, total)

    return chunks


def _extract_overlap(text: str, model_name: str, overlap_tokens: int) -> str:
    """Extract approximately overlap_tokens worth of text from the end."""
    if not text or overlap_tokens <= 0:
        return ""
    # Use sentence boundaries for cleaner overlap
    sentences = re.split(r'(?<=[.!?])\s+', text)
    overlap = ''
    for s in reversed(sentences):
        candidate = s + ' ' + overlap if overlap else s
        if _token_count(candidate, model_name) > overlap_tokens * 2:
            break
        overlap = candidate
    return overlap.strip()


def _sentence_split_chunk(
    text: str, model_name: str, target_tokens: int, overlap_tokens: int
) -> List[str]:
    """Split a large paragraph into sentence-level chunks with overlap."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) <= 1:
        # Can't split further, just truncate
        return [_truncate_to_tokens(text, model_name, target_tokens)]

    subchunks: List[str] = []
    current: List[str] = []
    current_tokens = 0

    for s in sentences:
        s_tokens = _token_count(s, model_name)
        if current_tokens + s_tokens > target_tokens and current:
            subchunks.append(' '.join(current))
            # Overlap
            overlap = _extract_overlap(' '.join(current), model_name, overlap_tokens)
            current = [overlap] if overlap else []
            current_tokens = _token_count(overlap, model_name) if overlap else 0

        current.append(s)
        current_tokens += s_tokens

    if current:
        subchunks.append(' '.join(current))

    return subchunks


def _truncate_to_tokens(text: str, model_name: str, max_tokens: int) -> str:
    """Truncate text to max_tokens."""
    enc = _get_encoder(model_name)
    tokens = enc.encode(text)
    return enc.decode(tokens[:max_tokens])


def _format_chunk_content(chunk: Dict, idx: int, total: int) -> str:
    """
    Add lightweight provenance to a chunk.

    Only the section heading is included. We intentionally do NOT inject
    "PART x OF y" / "continues in part" markers: the model tends to echo those
    into its answer, leaking the internal chunking to the user. Chunk ordering
    is handled internally and multi-chunk results are synthesised into a single
    answer, so the model never needs to reference parts.
    """
    parts: List[str] = []

    heading = chunk.get('heading', '')
    if heading:
        parts.append(f"[Section: {heading}]")
        parts.append('')

    parts.append(chunk['content'])

    return '\n'.join(parts)


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
        if not isinstance(msg, dict):
            continue
        content = msg.get("content") or ""
        tokens = len(enc.encode(content))
        if running + tokens > budget_tokens:
            break
        trimmed.append(msg)
        running += tokens

    return list(reversed(trimmed))


def _history_to_text(previous_messages, limit=10):
    """Convert conversation history to a text block for prompt-based providers."""
    if not previous_messages or not isinstance(previous_messages, list):
        return ""
    lines = []
    for msg in previous_messages[-limit:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            lines.append(f"{role.capitalize()}: {content}")
    return "\n".join(lines)


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

def _get_encoder(model: str):
    """Get tiktoken encoder for a model, falling back to cl100k_base."""
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


_FAMILY_CONTEXT_WINDOWS = (
    ("gpt-4.1", 128000),
    ("gpt-4o", 128000),
    ("gpt-4-turbo", 128000),
    ("gpt-4", 128000),
    ("gpt-5", 128000),
    ("gpt-3.5", 16000),
    ("o4", 128000),
    ("o3", 128000),
    ("o1", 128000),
    ("claude", 200000),
    ("gemini", 128000),
    ("llama-3.1", 128000),
    ("llama-3.2", 128000),
    ("llama-3.3", 128000),
    ("qwen2.5", 32768),
    ("qwen", 32768),
    ("mistral", 32768),
    ("phi-3", 4096),
)


def get_token_limit(model: str) -> int:
    if not model:
        return MODEL_CONTEXT_WINDOWS["default"]
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]
    name = model.split("/")[-1].lower()
    for key, limit in MODEL_CONTEXT_WINDOWS.items():
        if key.lower() == name:
            return limit
    for prefix, limit in _FAMILY_CONTEXT_WINDOWS:
        if name.startswith(prefix):
            return limit
    return MODEL_CONTEXT_WINDOWS["default"]


def fit_messages_to_context(previous_messages, model: str, reserve_tokens: int = 6000):
    """Keep the newest messages that fit inside the provider context window."""
    limit = get_token_limit(model or "")
    budget = max(limit - reserve_tokens, 512)
    return _trim_messages_to_budget(previous_messages, model or "gpt-4o-mini", budget)


def _build_messages_for_openai(
    previous_messages: Optional[List[dict]],
    chunk: str,
    chunk_info: Optional[Dict] = None,
    query_text: str = "",
    model: str = "gpt-4o-mini",
    accumulated_context: str = "",
) -> List[dict]:
    """
    Construct OpenAI messages with semantic chunk metadata and accumulated cross-chunk context.
    """
    token_limit = get_token_limit(model)

    # Build enhanced system prompt
    system_prompt = (
        "You are a precise, concise document analysis and QA assistant.\n"
        "- Answer based on the document excerpt and the conversation context.\n"
        "- Maintain continuity with previous exchanges — you CAN refer back to earlier Q&A.\n"
        "- Cite specific section names or headings when helpful.\n"
        "- Write as if analysing a single document. Do NOT mention 'parts', 'chunks', "
        "'excerpts', or 'sections 1/2/3 of N' — the user must not see the document was "
        "processed in pieces.\n"
        "- If the answer requires information not in the current excerpt, say so clearly."
    )

    # Accumulated context from previous chunks (cross-chunk awareness)
    context_prefix = ""
    if accumulated_context:
        context_prefix = (
            f"[FINDINGS FROM PREVIOUS DOCUMENT SECTIONS]\n{accumulated_context}\n\n"
            f"[CURRENT SECTION - the chunk you should answer from now]\n\n"
        )

    # Reserve space for system + current prompt + output
    reserve_for_output = 2048
    reserve_for_prompt = (
        _token_count(system_prompt, model)
        + _token_count(context_prefix, model)
        + _token_count(chunk, model)
        + _token_count(query_text, model)
        + 300
    )
    budget_for_history = max(token_limit - reserve_for_output - reserve_for_prompt, 256)

    trimmed_history = _trim_messages_to_budget(previous_messages, model, budget_for_history)

    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(trimmed_history)

    content_parts = [context_prefix + chunk]
    if query_text:
        content_parts.append(
            f"Question: {query_text}\n\n"
            f"Answer briefly, cite section headings where helpful, and say if the answer is "
            f"uncertain. Do not refer to parts, chunks, or excerpt numbers."
        )
    messages.append({"role": "user", "content": "\n\n".join(content_parts)})
    return messages


def _openai_chat_completion(client, *, model, messages, max_tokens, temperature, timeout):
    """Create a chat completion against an OpenAI-compatible endpoint, resilient
    to model-specific parameter restrictions.

    Newer reasoning/"intelligence" models (o1/o3/gpt-5.x, and whatever comes
    next) reject a custom ``temperature`` ("Only the default (1) value is
    supported") and require ``max_completion_tokens`` instead of ``max_tokens``.
    LiteLLM's ``drop_params`` does not reliably handle these on the proxy
    (BerriAI/litellm #16090, #21911), so we adapt here by retrying without the
    offending parameter. This is model-agnostic: no name list to maintain.
    """
    kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout": timeout,
    }
    for _ in range(3):  # at most: drop temperature, then swap max_tokens
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            msg = str(e).lower()
            if "temperature" in msg and "temperature" in kwargs:
                # Reasoning models only accept the default temperature.
                kwargs.pop("temperature", None)
                continue
            if "max_tokens" in msg and "max_tokens" in kwargs:
                # Some models require max_completion_tokens instead.
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                continue
            raise
    # Final attempt (surfaces the real error if it still fails).
    return client.chat.completions.create(**kwargs)


def dispatch_to_llm(
    query_text: str,
    chunk: str,
    llm_config: dict,
    previous_messages: Optional[List[dict]] = None,
    *,
    accumulated_context: str = "",
) -> str:
    """
    Provider-agnostic dispatcher that returns a STRING response.
    Supported providers: 'openai', 'ollama', 'vllm'
    """
    # Sanitize inputs to prevent type errors
    if not isinstance(llm_config, dict):
        llm_config = {}
    if not isinstance(chunk, str):
        chunk = str(chunk) if chunk else ""
    if not isinstance(query_text, str):
        query_text = str(query_text) if query_text else ""
    if previous_messages is None:
        previous_messages = []
    if isinstance(previous_messages, str):
        try:
            previous_messages = json.loads(previous_messages)
        except Exception:
            previous_messages = []
    if not isinstance(previous_messages, list):
        previous_messages = []
    # Ensure every item is a dict with role/content
    previous_messages = [
        m for m in previous_messages
        if isinstance(m, dict) and m.get("content")
    ]

    provider = (llm_config.get("provider") or "openai").lower()
    model = llm_config.get("model") or "gpt-3.5-turbo"
    temperature = float(llm_config.get("temperature", 0.2))
    timeout = int(llm_config.get("timeout", 120))

    # A client-supplied endpoint pointing at localhost/127.0.0.1 resolves to this
    # container, not the model server. Drop it so env/in-cluster defaults apply.
    _ep = llm_config.get("endpoint")
    if _ep and ("localhost" in _ep or "127.0.0.1" in _ep):
        llm_config = {**llm_config, "endpoint": None}

    if provider == "openai":
        api_key = llm_config.get("api_key")
        client = OpenAI(api_key=api_key) if api_key else OpenAI()

        # Reuse your existing helper to respect token budgets
        messages = _build_messages_for_openai(
            previous_messages=previous_messages,
            chunk=chunk,
            query_text=query_text,
            model=model,
            accumulated_context=accumulated_context,
        )
        try:
            response = _openai_chat_completion(
                client,
                model=model,
                messages=messages,
                max_tokens=1024,
                temperature=temperature,
                timeout=timeout,
            )
            text = response.choices[0].message.content or ""
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
            ctx_prefix = f"[FINDINGS FROM PREVIOUS DOCUMENT SECTIONS]\n{accumulated_context}\n\n" if accumulated_context else ""
            prompt_parts = [
                "You are a precise, concise document QA assistant. Answer using only the document facts. "
                "If unsure, say so. Cite section/part references when available.",
                ("Conversation so far:\n" + history_text) if history_text else None,
                ctx_prefix + "Document:\n" + (chunk or "")[:200000],
                "Task:\nAnswer the user question using only the document facts.",
                "Question:\n" + (query_text or ""),
                "Answer:"
            ]
            prompt = "\n\n".join([p for p in prompt_parts if p])

            text = (chat | StrOutputParser()).invoke(prompt)
            return text if isinstance(text, str) else str(text)

        except Exception as e:
            raise RuntimeError(f"Ollama invocation failed (url={base_url}, model={model}): {e}")

    elif provider == "litellm":
        # LiteLLM proxy: one OpenAI-compatible endpoint in front of every provider
        from core.llm_providers import resolve_endpoint, resolve_api_key, openai_compatible_base

        base_url = openai_compatible_base("litellm", resolve_endpoint("litellm", llm_config.get("endpoint")))
        # The gateway is always called with the platform's own LiteLLM key (resolve_api_key).
        api_key = resolve_api_key("litellm", llm_config.get("api_key")) or "EMPTY"
        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            messages = _build_messages_for_openai(
                previous_messages=previous_messages,
                chunk=chunk,
                query_text=query_text,
                model=model,
                accumulated_context=accumulated_context,
            )
            response = _openai_chat_completion(
                client, model=model, messages=messages, max_tokens=1024, temperature=temperature, timeout=timeout,
            )
            text = response.choices[0].message.content or ""
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
            messages = _build_messages_for_openai(
                previous_messages=previous_messages,
                chunk=chunk,
                query_text=query_text,
                model=model,
                accumulated_context=accumulated_context,
            )
            response = _openai_chat_completion(
                client,
                model=model,
                messages=messages,
                max_tokens=1024,
                temperature=temperature,
                timeout=timeout,
            )
            text = response.choices[0].message.content or ""
            return text if isinstance(text, str) else str(text)
        except Exception as e:
            raise RuntimeError(f"vLLM invocation failed (url={base_url}, model={model}): {e}")

    elif provider == "litellm":
        # LiteLLM proxy — OpenAI-compatible, routes to any provider
        base_url = (
            llm_config.get("endpoint")
            or os.getenv("LITELLM_URL")
            or "http://localhost:4000/v1"
        )
        # Authenticate to the LiteLLM gateway with the server-side LiteLLM key
        # (master/virtual key). A user-supplied key targets an underlying provider
        # and must not be sent as the gateway token — LiteLLM would reject it.
        api_key = os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or "sk-litellm"

        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            messages = _build_messages_for_openai(
                previous_messages=previous_messages,
                chunk=chunk,
                query_text=query_text,
                model=model,
                accumulated_context=accumulated_context,
            )
            response = _openai_chat_completion(
                client,
                model=model,
                messages=messages,
                max_tokens=1024,
                temperature=temperature,
                timeout=timeout,
            )
            text = response.choices[0].message.content or ""
            return text if isinstance(text, str) else str(text)
        except Exception as e:
            raise RuntimeError(f"LiteLLM invocation failed (url={base_url}, model={model}): {e}")

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
        import os
        ext = os.path.splitext(file_path)[1] or '(unknown)'
        raise ValueError(
            f"Could not extract text from this {ext} file. "
            f"The file may be corrupted or in a format that requires "
            f"specialized processing. Please try re-uploading the file."
        )
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


def _score_chunk_relevance(chunk: Dict, query: str, model: str) -> float:
    """
    Quick keyword-based relevance score for a chunk vs query.
    Returns 0-1 score based on word overlap.
    """
    chunk_words = set(re.findall(r'\w+', chunk['content'].lower()))
    query_words = set(re.findall(r'\w+', query.lower()))
    if not query_words:
        return 0.5
    # Score based on word overlap, boosted by heading match
    score = len(chunk_words & query_words) / len(query_words)
    heading = chunk.get('heading', '')
    if heading:
        heading_words = set(re.findall(r'\w+', heading.lower()))
        score += 0.3 * (len(heading_words & query_words) / max(len(query_words), 1))
    return score


def execute_file_query(
    query_text: str,
    file_path: Optional[str],
    llm_config: dict,
    previous_messages: Optional[List[dict]] = None,
    *,
    file_id: Optional[int] = None
) -> str:
    """
    Smart document QA with semantic chunking and chunk relevance ranking.
    """
    # Sanitize inputs
    if not isinstance(llm_config, dict):
        llm_config = {}
    if previous_messages is None:
        previous_messages = []
    if isinstance(previous_messages, str):
        try:
            previous_messages = json.loads(previous_messages)
        except Exception:
            previous_messages = []
    if not isinstance(previous_messages, list):
        previous_messages = []
    # Ensure every item is a valid message dict
    previous_messages = [
        m for m in previous_messages
        if isinstance(m, dict) and m.get("content")
    ]

    if file_id is not None:
        content = _get_or_build_cached_text_for_file_id(file_id)
    elif file_path:
        content = _materialize_text_from_file_path(file_path)
    else:
        raise ValueError("Either file_id or file_path must be supplied.")

    model_name = llm_config.get("model", "gpt-4o-mini")

    # Use larger chunks to reduce API calls (8000 tokens per chunk)
    CHUNK_TOKENS = 8000
    OVERLAP_TOKENS = 400
    MAX_CHUNKS = 5

    chunks = semantic_chunk(
        text=content,
        model_name=model_name,
        target_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )

    if not chunks:
        return "Document appears to be empty or could not be processed."

    print(f"[INFO] Semantic chunking: {len(content)} chars → {len(chunks)} chunks "
          f"(~{CHUNK_TOKENS} tokens each)")

    # Score and rank chunks by relevance to query
    for ch in chunks:
        ch['relevance'] = _score_chunk_relevance(ch, query_text, model_name)

    # Sort by relevance (highest first), keeping original index for ordering
    ranked = sorted(chunks, key=lambda c: c['relevance'], reverse=True)

    # Always include the first chunk (document intro) and last chunk if doc is large
    selected: List[Dict] = []
    seen_indices: set = set()

    # Add first chunk (document intro/heading)
    first = chunks[0]
    selected.append(first)
    seen_indices.add(0)

    # Add top relevant chunks
    for ch in ranked:
        if len(selected) >= MAX_CHUNKS:
            break
        idx = ch['index'] - 1  # 0-based
        if idx not in seen_indices:
            selected.append(ch)
            seen_indices.add(idx)

    # Add last chunk if document is long and we have room
    if len(chunks) > 2 and len(selected) < MAX_CHUNKS:
        last = chunks[-1]
        if (last['index'] - 1) not in seen_indices:
            selected.append(last)

    # Sort selected chunks back to document order
    selected.sort(key=lambda c: c['index'])

    total_tokens = sum(_token_count(c['content'], model_name) for c in selected)
    print(f"[INFO] Selected {len(selected)}/{len(chunks)} chunks "
          f"(~{total_tokens} total tokens) for LLM processing")

    answers: List[str] = []          # clean, successful partial answers
    errors: List[str] = []           # per-chunk error notes (if any)
    accumulated_context = ""

    for i, chunk in enumerate(selected):
        try:
            print(f"[INFO] Processing chunk {chunk['index']}/{len(chunks)} "
                  f"(relevance {chunk.get('relevance', 0):.2f}) "
                  f"[{chunk.get('heading', 'no heading')}] ({len(chunk['content'])} chars)")
            answer = dispatch_to_llm(
                query_text=query_text,
                chunk=chunk['content'],
                llm_config=llm_config,
                previous_messages=previous_messages,
                accumulated_context=accumulated_context,
            )

            answers.append((answer or "").strip())

            if i < len(selected) - 1:
                accumulated_context += f"Section {chunk['index']} findings: {answer[:400]}\n"

        except Exception as e:
            import traceback
            print(f"[ERROR] Failed to process chunk {chunk['index']}: {e}")
            traceback.print_exc()
            errors.append(f"[Error processing section {chunk['index']}: {e}]")

    # Single successful section (or single selected chunk): no merge needed.
    if len(answers) <= 1:
        result = answers[0] if answers else ""
        if errors:
            result = (result + "\n\n" + "\n\n".join(errors)).strip()
        return result or "No answer could be generated for this document."

    # Multiple sections → synthesize ONE cohesive answer (map-reduce style),
    # so the user never sees "Part X of Y" fragments.
    final = _synthesize_answers(query_text, answers, llm_config)

    if errors:
        final = (final + "\n\n" + "\n\n".join(errors)).strip()
    return final


def _synthesize_answers(query_text: str, partial_answers: List[str], llm_config: dict) -> str:
    """Merge per-section answers into a single coherent markdown response.

    Uses one additional LLM call. Falls back to a de-duplicated concatenation
    (without any "Part X of Y" markers) if the synthesis call fails.
    """
    combined = "\n\n".join(
        f"### Findings from section {i + 1}\n{ans}"
        for i, ans in enumerate(partial_answers)
    )

    synthesis_instruction = (
        "You are given partial answers that were computed independently from different "
        "sections of the SAME source document, each responding to this user request:\n\n"
        f"\"{query_text}\"\n\n"
        "Merge them into ONE cohesive, non-repetitive answer that reads as a single response. "
        "Remove duplicated preamble and overlapping statements, reconcile any conflicts, and "
        "preserve every distinct fact. Do NOT mention 'parts', 'sections', 'chunks', or that "
        "the answer was assembled from pieces. Format professionally in clean Markdown: use "
        "headings, **bold** for key terms, and bullet lists or tables where they aid clarity."
    )

    try:
        return dispatch_to_llm(
            query_text=synthesis_instruction,
            chunk=combined,
            llm_config=llm_config,
            previous_messages=None,
            accumulated_context="",
        ).strip()
    except Exception as e:
        import traceback
        print(f"[WARN] Answer synthesis failed, falling back to concatenation: {e}")
        traceback.print_exc()
        # Clean fallback: join distinct answers without any part labels.
        return "\n\n".join(partial_answers).strip()


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
        column_names = fetch_column_names(url, table_name)   # sensitive columns already removed
        if not column_names:
            return "No non-sensitive columns are available to query on this table."
        sql_query = generate_sql_query(table_name, query_text, column_names, llm_config) or fallback_select(url, table_name)
        result = execute_sql_query(url, sql_query, stream=stream, chunk_size=chunk_size)   # re-vetted, sensitive columns dropped

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

        # Semantic-chunk the data for LLM, reserve room for answer
        model_name = llm_config.get("model", "gpt-4o-mini")
        chunks = semantic_chunk(
            text=text_data,
            model_name=model_name,
            target_tokens=3000,
            overlap_tokens=300,
        )

        if not chunks:
            return "No data to analyze."

        answers = []
        accumulated_context = ""
        for i, chunk in enumerate(chunks):
            try:
                print(f"[INFO] Sending chunk {chunk['index']}/{chunk['total']} to LLM...")
                answer = dispatch_to_llm(
                    query_text=query_text,
                    chunk=chunk['content'],
                    llm_config=llm_config,
                    previous_messages=previous_messages,
                    accumulated_context=accumulated_context,
                )
                answers.append(f"[Part {chunk['index']}]: {answer}")
                if i < len(chunks) - 1:
                    accumulated_context += f"Part {chunk['index']} findings: {answer[:500]}\n"
            except Exception as e:
                print(f"[ERROR] Failed to process chunk {chunk['index']}: {e}")
                answers.append(f"[Error processing chunk: {e}]")

        return "\n\n---\n\n".join(answers)

    except UnsafeTarget as e:
        return f"[ERROR] {e}"
    except Exception as e:
        logger.warning("DocuChat DB query failed: %s", type(e).__name__)
        return "[ERROR] The database query failed. Check the connection details and table name."

