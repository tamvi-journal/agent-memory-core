"""B1 differential rows: actual CPython C json decoder and strict UTF-8 outcomes."""
from __future__ import annotations
import json
import math
import sys
import unicodedata


def canonical(value):
    if value is None: return {"kind": "null"}
    if isinstance(value, bool): return {"kind": "bool", "value": value}
    if isinstance(value, str): return {"kind": "string", "codepoints": [ord(c) for c in value]}
    if isinstance(value, int): return {"kind": "int", "value": str(value)}
    if isinstance(value, float):
        return {"kind": "float", "value": "nan" if math.isnan(value) else value.hex()}
    if isinstance(value, list): return {"kind": "array", "items": [canonical(item) for item in value]}
    return {"kind": "object", "entries": [[canonical(key), canonical(item)] for key, item in value.items()]}


def differential_table(oracle_commit):
    rows = []
    def add(decoder, data, label):
        try:
            text = data.decode("utf-8", errors="strict")
            value = json.loads(text) if decoder == "json" else text
            result = {"ok": canonical(value)}
        except (ValueError, UnicodeError) as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        rows.append({"id": label, "decoder": decoder, "input_hex": data.hex(), "outcome": result})
    # Each C-decoder syntax category has distinct offsets and a non-BMP/newline case.
    # The BOM diagnostic necessarily has offset zero: JSONDecoder only checks s[0].
    snippets = ["", "?", "[", '[1,?]', '["😀",\n ?]', '{', '{x:0}', '{"😀":0,\n x:1}',
        '{"a" 1}', '{"😀":0,\n"b" ?}', '[1 2]', '{"😀":0,\n"b":1 "c":2}',
        '"a\x00"', '["😀",\n"a\x1f"]', '"a\\q"', '["😀",\n"a\\?"]',
        '"a\\u12"', '["😀",\n"a\\uX234"]', '"\\ud800\\u123X"',
        '"abc', '["😀",\n"ab\\"c', '"x\\', '0 1', '"😀"\n {}', '\ufeff{}', '\ufeff \n{}',
        '01', '-01', '1.', '1e', '1e+', '-NaN', '+Infinity', 'NAN', 'Infinityz', '.', '--1',
        '{"a":1,"b":2,"a":3}', '{"😀":0,\n"x":NaN,"x":Infinity}',
        'NaN', 'Infinity', '-Infinity', '[NaN,Infinity,-Infinity,1e9999,-1e9999,-0,-0.0]',
        '"\\ud800"', '"\\udfff"', '"\\ud800\\udc00"', '"\\ud800\\u0041"',
        '"escaped \\" and \\\\ and /"', '"\\b\\f\\n\\r\\t"', '[null,true,false,1,1.0,"😀"]',
        '9'*4300, '9'*4301]
    for n, text in enumerate(snippets): add("json", text.encode("utf-8"), f"json-{n:03}")
    sequences = [bytes([byte]) for byte in range(256)]
    for lead, lo, hi, width in [(0xC2,0x80,0xBF,2),(0xDF,0x80,0xBF,2),
        (0xE0,0xA0,0xBF,3),(0xED,0x80,0x9F,3),(0xE1,0x80,0xBF,3),(0xEF,0x80,0xBF,3),
        (0xF0,0x90,0xBF,4),(0xF4,0x80,0x8F,4),(0xF1,0x80,0xBF,4),(0xF3,0x80,0xBF,4)]:
        for second in sorted({0x7F,lo-1,lo,lo+1,hi-1,hi,hi+1,0xC0}):
            sequences.append(bytes([lead,second]+[0x80]*(width-2)))
        good = bytes([lead,lo]+[0x80]*(width-2))
        for end in range(1,width): sequences.append(good[:end])
        for position in range(2,width):
            for byte in (0x7F,0x80,0xBF,0xC0,0xFF):
                changed = bytearray(good); changed[position] = byte; sequences.append(bytes(changed))
    sequences += [b'\xc2A\xff', b'\xe1\x80A\xc2', b'\xf1\x80\x80A\xe0', b'\xed\xa0\x80',
                  b'\xf4\x90\x80\x80', b'\xef\xbb\xbf{}']
    for n, data in enumerate(dict.fromkeys(sequences)):
        for label, prefix in [("zero", b""), ("offset", "A😀\n".encode())]:
            add("utf8", prefix + data, f"utf8-{n:03}-{label}")
    return {"schema": "trajecta.cli-decode-outcomes/v1", "generator": "tools/golden_cli/decode.py",
            "oracle_commit": oracle_commit, "python": sys.version.split()[0],
            "unicode": unicodedata.unidata_version, "rows": rows}
