from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import urllib.request
import urllib.error
import json as _json
import ssl
import os
import logging

logger = logging.getLogger(__name__)


@csrf_exempt
def proxy_llm_models(request):
    try:
        body = _json.loads(request.body) if request.body else {}
    except Exception:
        body = {}

    provider = (body.get('provider') or request.GET.get('provider') or '').lower()
    endpoint = body.get('endpoint') or request.GET.get('endpoint')
    api_key = body.get('api_key') or request.GET.get('api_key')

    # A client-supplied endpoint pointing at localhost/127.0.0.1 is meaningless
    # inside this container (it resolves to the container itself, not the model
    # server), so ignore it and fall back to the in-cluster defaults / env vars.
    if endpoint and ('localhost' in endpoint or '127.0.0.1' in endpoint):
        endpoint = None

    # For the LiteLLM gateway, authentication is always the server-side LiteLLM
    # key (master/virtual key). A user-supplied key is for an underlying provider
    # and must NOT be forwarded as the gateway Bearer token — LiteLLM would try to
    # validate it as a virtual key and fail ("No connected db") -> 400 -> 502.
    if provider == 'litellm':
        api_key = os.environ.get('LITELLM_API_KEY') or os.environ.get('LITELLM_MASTER_KEY', '')
    # Fall back to environment variables for API keys
    elif not api_key:
        if provider == 'openai':
            api_key = os.environ.get('OPENAI_API_KEY', '')

    if provider == 'ollama':
        base = (endpoint or os.environ.get('OLLAMA_URL', 'http://aidocumines_ollama:11434')).rstrip('/')
        url = f'{base}/api/tags'
    elif provider == 'openai':
        base = (endpoint or 'https://api.openai.com/v1').rstrip('/')
        url = f'{base}/models'
    elif provider in ('litellm', 'vllm'):
        defaults = {
            'vllm': os.environ.get('VLLM_URL', 'http://localhost:8000/v1'),
            'litellm': os.environ.get('LITELLM_URL', 'http://localhost:4000/v1'),
        }
        base = (endpoint or defaults.get(provider, 'http://localhost:4000/v1')).rstrip('/')
        url = f'{base}/models'
    else:
        return JsonResponse({'error': f'Unsupported provider: {provider}'}, status=400)

    req_headers = {}
    if api_key and provider != 'ollama':
        req_headers['Authorization'] = f'Bearer {api_key}'

    logger.info(
        "proxy_llm_models: provider=%s url=%s has_api_key=%s",
        provider, url, bool(api_key),
    )

    try:
        req = urllib.request.Request(url, headers=req_headers)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = _json.loads(resp.read())
            # Drop wildcard placeholder entries (e.g. "openai/*") that LiteLLM
            # returns for provider-wildcard routing — they are not selectable
            # models and would break a chat request if picked from the dropdown.
            if isinstance(data, dict) and isinstance(data.get('data'), list):
                data['data'] = [
                    m for m in data['data']
                    if not (isinstance(m, dict) and '*' in str(m.get('id', '')))
                ]
            return JsonResponse(data, safe=False)
    except urllib.error.HTTPError as e:
        # Upstream responded with an error status (e.g. 401 invalid key).
        try:
            detail = e.read().decode('utf-8', 'replace')
        except Exception:
            detail = ''
        logger.error(
            "proxy_llm_models upstream HTTPError: provider=%s url=%s status=%s body=%s",
            provider, url, e.code, detail,
        )
        return JsonResponse(
            {'error': f'Upstream {e.code} from {provider}', 'detail': detail},
            status=502,
        )
    except Exception as e:
        # Network-level failure (DNS, connection refused, timeout, TLS, ...).
        logger.exception(
            "proxy_llm_models connection error: provider=%s url=%s", provider, url,
        )
        return JsonResponse(
            {'error': f'{type(e).__name__}: {e}', 'provider': provider, 'url': url},
            status=502,
        )
