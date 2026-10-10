import pytest
from tools.r3.fixture_headers import compare


def fixture(tmp_path):
    a, b = tmp_path / "old", tmp_path / "new"
    a.mkdir(); b.mkdir()
    raw = bytearray(b"SQLite format 3\0" + b"a" * (8192 - 16))
    (a / "store.sqlite3").write_bytes(raw)
    return a, b, raw


def test_only_eight_header_bytes_are_permitted(tmp_path):
    old, new, raw = fixture(tmp_path)
    for offset in (*range(24, 28), *range(92, 96)):
        raw[offset] = 1
    (new / "store.sqlite3").write_bytes(raw)
    assert len(compare(old, new)) == 1


@pytest.mark.parametrize("change", ["header", "page", "length", "dump", "inventory"])
def test_other_payload_changes_fail_closed(tmp_path, change):
    old, new, raw = fixture(tmp_path)
    if change == "header": raw[23] = 1
    if change == "page": raw[8191] = 1
    if change == "length": raw.extend(b"a")
    (new / "store.sqlite3").write_bytes(raw)
    if change == "dump":
        (old / "dump.json").write_bytes(b"{}\n")
        (new / "dump.json").write_bytes(b"{} ")
    if change == "inventory": (new / "extra").write_bytes(b"")
    with pytest.raises(ValueError): compare(old, new)


def json_fixture(tmp_path):
    import hashlib
    old, new, raw = fixture(tmp_path)
    (old / "case").mkdir(); (new / "case").mkdir()
    (old / "store.sqlite3").rename(old / "case" / "store.sqlite3")
    original = bytes(raw)
    raw[24] = 1
    (new / "case" / "store.sqlite3").write_bytes(raw)
    a, b = hashlib.sha256(original).hexdigest(), hashlib.sha256(raw).hexdigest()
    before = ('{"tree":{"store.sqlite3":{"unchanged_sha256":"%s"}},"databases":{"store.sqlite3":{"oracle_sha256":"%s"}},"frozen":"é"}\n' % (a, a)).encode()
    after = before.replace(a.encode(), b.encode())
    (old / "case" / "expected.json").write_bytes(before)
    (new / "case" / "expected.json").write_bytes(after)
    return old, new, a, b


def test_all_validated_hash_fields_masked_together(tmp_path):
    old, new, _, _ = json_fixture(tmp_path)
    proof = compare(old, new)
    hashes = [row for row in proof if "json_path" in row]
    assert [row["json_path"] for row in hashes] == [
        ["tree", "store.sqlite3", "unchanged_sha256"],
        ["databases", "store.sqlite3", "oracle_sha256"],
    ]
    assert all(row["reference_file"] == "case/store.sqlite3" for row in hashes)


@pytest.mark.parametrize("change", ["wrong-old", "wrong-new", "spacing", "order", "extra", "unrelated-hash", "unproven-file"])
def test_derived_hash_permission_is_exact(tmp_path, change):
    old, new, a, b = json_fixture(tmp_path)
    left = old / "case" / "expected.json"
    right = new / "case" / "expected.json"
    if change == "wrong-old": left.write_bytes(left.read_bytes().replace(a.encode(), b"0" * 64))
    if change == "wrong-new": right.write_bytes(right.read_bytes().replace(b.encode(), b"0" * 64))
    if change == "spacing": right.write_bytes(right.read_bytes().replace(b'"tree":', b'"tree": '))
    if change == "order":
        import json
        obj = json.loads(right.read_bytes())
        right.write_text(json.dumps(dict(reversed(list(obj.items()))), ensure_ascii=False) + "\n")
    if change == "extra": right.write_bytes(right.read_bytes().replace('"é"'.encode(), b'"x"'))
    if change == "unrelated-hash":
        left.write_bytes(left.read_bytes().replace(b'"frozen":"', b'"frozen":"' + a.encode()))
        right.write_bytes(right.read_bytes().replace(b'"frozen":"', b'"frozen":"' + b.encode()))
    if change == "unproven-file":
        raw = bytearray((new / "case" / "store.sqlite3").read_bytes()); raw[8191] = 1
        (new / "case" / "store.sqlite3").write_bytes(raw)
    with pytest.raises(ValueError): compare(old, new)


def test_transient_database_references_require_retained_mask_proven_bytes(tmp_path):
    import json
    old, new, a, b = json_fixture(tmp_path)
    before, after = tmp_path / "old-outputs", tmp_path / "new-outputs"
    (before / "case").mkdir(parents=True); (after / "case").mkdir(parents=True)
    for corpus, references in [(old, before), (new, after)]:
        (references / "case" / "store.sqlite3").write_bytes((corpus / "case" / "store.sqlite3").read_bytes())
        (corpus / "case" / "store.sqlite3").unlink()
    proof = compare(old, new, old_references=before, new_references=after)
    assert len(proof) == 2
    with pytest.raises(ValueError): compare(old, new)
    raw = bytearray((after / "case" / "store.sqlite3").read_bytes()); raw[8191] = 1
    (after / "case" / "store.sqlite3").write_bytes(raw)
    with pytest.raises(ValueError): compare(old, new, old_references=before, new_references=after)


def test_duplicate_keys_cannot_hide_a_fixture_json_change(tmp_path):
    old, new, _, _ = json_fixture(tmp_path)
    for corpus in (old, new):
        path = corpus / "case" / "expected.json"
        path.write_bytes(path.read_bytes().replace(b'"frozen":"', b'"frozen":"discarded","frozen":"'))
    with pytest.raises(ValueError): compare(old, new)
