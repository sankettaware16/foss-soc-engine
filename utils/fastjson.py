"""
Fast JSON helpers with a safe fallback.

Uses orjson when available (2-5x faster for both parse and serialize), and
transparently falls back to the standard library `json` if orjson is not
installed. The engine never hard-requires orjson, so installs stay simple.

API:
    loads(s)        -> parse bytes or str into Python objects
    dumps(obj)      -> compact JSON string
    dumps_bytes(obj)-> compact JSON as UTF-8 bytes (fastest path for file writes)
"""

try:
    import orjson

    HAVE_ORJSON = True

    def loads(s):
        # orjson.loads accepts both bytes and str
        return orjson.loads(s)

    def dumps_bytes(obj):
        return orjson.dumps(obj)

    def dumps(obj):
        return orjson.dumps(obj).decode("utf-8")

except Exception:  # orjson missing or failed to import
    import json as _json

    HAVE_ORJSON = False

    def loads(s):
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", "ignore")
        return _json.loads(s)

    def dumps_bytes(obj):
        return _json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8", "ignore"
        )

    def dumps(obj):
        return _json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
