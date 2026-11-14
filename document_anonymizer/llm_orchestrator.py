def query_model(blocks, element_ids, model="phi-2", api_key=None):
    """
    Accepts a list of structured blocks and element_ids to summarize.
    Supports:
    - OpenAI models (gpt-3.5, gpt-4) if `api_key` is provided
    - Local Ollama models (e.g., phi-2, deepseek-coder)
    """
    selected_blocks = [blk for blk in blocks if blk.get("element_id") in element_ids]
    prompt = "\n\n".join([blk["text"] for blk in selected_blocks])

    if not prompt.strip():
        return "No content to summarize."

    if model in ["gpt-3.5", "gpt-4o"]:
        if not api_key:
            return "OpenAI API key is required for remote models."
        try:
            import openai
            openai.api_key = api_key
            response = openai.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": f"Summarize the following:\n\n{prompt}"}]
            )
            from home_dash_analytics.agents.usage import record_response as _record_ai
            _record_ai(response, provider='openai', model=getattr(response, 'model', ''), task='anonymisation')
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"OpenAI API error: {str(e)}"

    else:
        # Local Ollama model inference
        try:
            import requests
            res = requests.post(
                "http://ollama:11434/api/generate",
                json={"model": model, "prompt": f"Summarize the following:\n\n{prompt}", "stream": False},
                timeout=30
            )
            if res.status_code == 200:
                return res.json().get("response", "No response returned by Ollama.")
            return f"Ollama error: {res.status_code} - {res.text}"
        except Exception as e:
            return f"Local inference error: {str(e)}"

