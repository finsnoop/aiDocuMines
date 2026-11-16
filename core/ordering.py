"""Server-side column sorting for paginated tables.

The browser asks for ``?ordering=<key>`` or ``?ordering=-<key>`` (descending). Only keys a view lists
are accepted; anything else falls back to the view's default, so user input never names a database field.
"""


def ordering_from(request, allowed: dict, default: str) -> list:
    """Order-by arguments for this request: the chosen column, then the default as a stable tie-break."""
    raw = (request.query_params.get("ordering") or "").strip()
    desc = raw.startswith("-")
    field = allowed.get(raw.lstrip("-"))
    if not field:
        return [default, "-pk"] if default.lstrip("-") != "pk" else [default]
    chosen = ("-" if desc else "") + field
    return [chosen, default, "-pk"]
