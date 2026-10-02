"""R2d §5.1: template expected bytes; actual bytes are never normalized in replay."""
import json
import re

TOKENS = (b"{{ROOT}}", b"{{ORIGIN}}")
ALLOWED = {"argv.json", "env.json", "stdout", "stderr", "data/.last-profile"}


def template(raw: bytes, *, file: str, root: str, origin: str, context: str, separator=None):
    if any(token in raw for token in TOKENS):
        raise ValueError("literal run token in raw bytes")
    values = [(root, TOKENS[0]), (origin, TOKENS[1])]
    for value, token in values:
        if not value:
            continue
        forms = {value.encode(), json.dumps(value, ensure_ascii=False)[1:-1].encode()}
        if token == TOKENS[0]:
            parts = re.split(r"[/\\]", value)
            pattern = rb"(?:/|\\{1,2})".join(re.escape(part.encode()) for part in parts)
            for match in re.finditer(pattern, raw, flags=re.IGNORECASE):
                if match[0] not in forms:
                    raise ValueError("run-specific root in a different or partly escaped form")
    if file not in ALLOWED:
        if any(value and any(form in raw for form in (value.encode(), json.dumps(value, ensure_ascii=False)[1:-1].encode())) for value, _ in values):
            raise ValueError("run-specific value outside a named boundary")
        return raw, []
    if context not in {"raw-text", "json-string"}:
        raise ValueError("unknown token context")
    result = raw
    for value, token in values:
        if not value:
            continue
        encoded = (json.dumps(value, ensure_ascii=False)[1:-1] if context == "json-string" else value).encode()
        if context == "json-string" and encoded != value.encode() and value.encode() in result:
            raise ValueError("partly escaped run-specific value")
        offset = 0
        while (at := result.find(encoded, offset)) >= 0:
            tail = result[at + len(encoded):]
            separators = [b"/"] + ([b"\\\\" if context == "json-string" else b"\\"] if token == TOKENS[0] else [])
            terminators = [b'"', b"\n", b"\r"]
            if tail and not any(tail.startswith(x) for x in separators + terminators):
                raise ValueError("run-specific prefix match without a path boundary")
            if token == TOKENS[0] and re.search(rb'(?:/|\\{1,2})\.{1,2}(?:/|\\{1,2}|["\n\r]|$)', re.split(rb'["\n\r]', tail, maxsplit=1)[0]):
                raise ValueError("unresolved run-specific path")
            result = result[:at] + token + result[at + len(encoded):]
            offset = at + len(token)
    # Case drift is refused even where the exact value did not match.
    lowered = raw.lower()
    for value, _ in values:
        if value and value.encode().lower() in lowered and value.encode() not in raw:
            raise ValueError("run-specific value in a different case")
    if context == "json-string":
        result = re.sub(rb'(\{\{ROOT\}\}[^"\n\r]*)', lambda m: m[0].replace(b"\\\\", b"/"), result)
    else:
        result = re.sub(rb'(\{\{ROOT\}\}[^"\n\r]*)', lambda m: m[0].replace(b"\\", b"/"), result)
    listing = [{"file": file, "byte_offset": match.start(), "token": match[0].decode(), "context": context}
               for match in re.finditer(rb"\{\{(?:ROOT|ORIGIN)\}\}", result)]
    if render(result, listing, file=file, root=root, origin=origin, separator=separator) != raw:
        raise ValueError("template did not render back to original bytes")
    return result, listing


def render(raw: bytes, listing: list, *, file: str, root: str, origin: str, separator=None):
    import os
    separator = separator or os.sep
    entries = sorted((x for x in listing if x["file"] == file), key=lambda x: x["byte_offset"])
    actual = [(m.start(), m[0].decode()) for m in re.finditer(rb"\{\{(?:ROOT|ORIGIN)\}\}", raw)]
    if actual != [(x["byte_offset"], x["token"]) for x in entries]:
        raise ValueError("token registry does not match template")
    if entries and file not in ALLOWED:
        raise ValueError("token outside named boundary")
    result = raw
    for entry in reversed(entries):
        at = entry["byte_offset"]
        token = entry["token"].encode()
        context = entry["context"]
        if context not in {"raw-text", "json-string"}:
            raise ValueError("unknown token context")
        value = root if token == TOKENS[0] else origin
        if not value:
            raise ValueError("missing run token value")
        end = at + len(token)
        tail = result[end:]
        if tail and not any(tail.startswith(x) for x in (b"/", b'"', b"\n", b"\r")):
            raise ValueError("token without a permitted boundary")
        if token == TOKENS[0]:
            tail = result[end:]
            if tail.startswith(b"/"):
                suffix = re.split(rb'["\n\r]', tail, maxsplit=1)[0]
                value += suffix.decode().replace("/", separator)
                end += len(suffix)
        if context == "json-string":
            value = json.dumps(value, ensure_ascii=False)[1:-1]
        result = result[:at] + value.encode() + result[end:]
    return result
