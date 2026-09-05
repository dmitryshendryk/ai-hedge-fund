"""Custom JSONResponse that replaces NaN / +inf / -inf with null at the wire.

FastAPI's default Starlette JSONResponse calls `json.dumps(...)` with default
`allow_nan=True`, which renders `NaN` / `Infinity` literals — invalid JSON
that browsers (and Python's own json parser) reject. We instead set
`allow_nan=False` and pre-sanitize any non-finite floats anywhere in the
payload to `null`.

Apply by passing `default_response_class=SafeJSONResponse` to the
`FastAPI(...)` constructor — every route response then runs through the
sanitizer without per-route changes.
"""

import json
import math

from starlette.responses import JSONResponse


def _sanitize(value: object) -> object:
    """Recursively replace non-finite floats with None. Handles dict / list /
    tuple / set containers; pass-through for everything else (the json
    encoder rejects unknown types itself)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, set):
        return [_sanitize(v) for v in value]
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content: object) -> bytes:
        sanitized = _sanitize(content)
        return json.dumps(
            sanitized,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")
