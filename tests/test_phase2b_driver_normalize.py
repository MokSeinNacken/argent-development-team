"""FIX 3 regression: driver content normalization.

``_normalize_content`` must fully unwrap nested base64 so the broker always
scans/writes the true canonical content — a double-encoded
``base64(base64(x))`` payload must never slip past the deny scan unchanged.
"""

import base64

import pytest

from argent_core import Role, WorkspaceBroker, role_source

from smoke.phase2b_e2e import _normalize_content

LEAD = role_source(Role.LEAD)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _decode(s: str) -> str:
    return base64.b64decode(s, validate=True).decode("utf-8")


def test_double_encoded_fully_unwrapped():
    real = "the real file data"
    double = _b64(_b64(real))
    normalized = _normalize_content(double)
    assert _decode(normalized) == real


def test_plain_text_encoded_once():
    plain = "some plain text"
    normalized = _normalize_content(plain)
    assert normalized == _b64(plain)
    assert _decode(normalized) == plain


def test_single_base64_kept():
    real = "single layer content"
    single = _b64(real)
    normalized = _normalize_content(single)
    assert normalized == single
    assert _decode(normalized) == real


def test_invalid_base64_stray_whitespace_treated_as_text():
    text = "data with a space inside"
    normalized = _normalize_content(text)
    assert _decode(normalized) == text


def test_whitespace_around_base64_stripped():
    single = _b64("hello")
    normalized = _normalize_content(f"  {single}\n")
    assert normalized == single
    assert _decode(normalized) == "hello"


def test_unwrapped_denylisted_content_rejected_by_broker(tmp_path):
    # A double-encoded payload whose real content is deny-listed must be fully
    # unwrapped so the broker rejects it (never written to the target).
    real = "my password is hunter2"
    double = _b64(_b64(real))
    normalized = _normalize_content(double)
    assert _decode(normalized) == real  # fully unwrapped to the real text
    broker = WorkspaceBroker()
    patch = {"op": "write", "path": "a.txt", "content": normalized}
    res = broker.apply_patch_set(str(tmp_path), [patch], Role.IMPLEMENTER, LEAD)
    assert res.applied == []
    assert res.errors[0]["error"] == "content_denylist"
    assert not (tmp_path / "a.txt").exists()


def test_depth_cap_plus_one_still_canonical():
    # 5 layers: 4 unwraps in the normalizer + 1 decode in the broker land on
    # the canonical text (never still-encoded on disk).
    real = "five layers deep"
    payload = _b64(_b64(_b64(_b64(_b64(real)))))
    normalized = _normalize_content(payload)
    assert _decode(normalized) == real


def test_depth_cap_exhausted_rejected():
    # 6 layers exceed the effective decode depth; the input must be rejected
    # fail-closed instead of writing still-encoded bytes.
    real = "six layers deep"
    payload = _b64(_b64(_b64(_b64(_b64(_b64(real))))))
    with pytest.raises(ValueError, match="depth cap"):
        _normalize_content(payload)


def test_plaintext_whitespace_preserved_bytewise():
    # Leading indentation, blank lines and the final newline must survive
    # byte-for-byte (the stripped candidate is only used for recognition).
    plain = "    indented = 1\n\ndef f():\n    return 1\n"
    normalized = _normalize_content(plain)
    assert _decode(normalized) == plain


@pytest.mark.parametrize(
    "label,raw",
    [
        ("spaces", "   "),
        ("tabs", "\t\t"),
        ("single_newline", "\n"),
        ("blank_lines", "\n\n\n"),
    ],
)
def test_whitespace_only_plaintext_preserved(tmp_path, label, raw):
    # Whitespace-only input is plaintext and must survive byte-for-byte
    # (the empty stripped form must not count as consistent base64).
    target = tmp_path / label
    target.mkdir()
    normalized = _normalize_content(raw)
    assert _decode(normalized) == raw
    broker = WorkspaceBroker()
    patch = {"op": "write", "path": "w.txt", "content": normalized}
    res = broker.apply_patch_set(str(target), [patch], Role.IMPLEMENTER, LEAD)
    assert res.errors == []
    assert (target / "w.txt").read_text() == raw


def test_formatted_inner_layer_unwrapped_and_scanned(tmp_path):
    # A nested encoded layer padded with whitespace must be fully unwrapped
    # so the canonical content is scanned: with a high-signal marker the
    # broker rejects and nothing is written (never still-encoded on disk).
    real = "my password is hunter2"
    layer2 = _b64(real)
    formatted = f"  {layer2}\n"  # whitespace around the nested layer
    payload = _b64(formatted)
    normalized = _normalize_content(payload)
    assert _decode(normalized) == real  # fully unwrapped to the real text
    broker = WorkspaceBroker()
    patch = {"op": "write", "path": "a.txt", "content": normalized}
    res = broker.apply_patch_set(str(tmp_path), [patch], Role.IMPLEMENTER, LEAD)
    assert res.applied == []
    assert res.errors[0]["error"] == "content_denylist"
    assert not (tmp_path / "a.txt").exists()


def test_formatted_inner_layer_writes_canonical_text(tmp_path):
    # Same shape with ordinary content: the canonical text is written, not
    # the still-encoded (whitespace-padded) layer.
    real = "ordinary = 1\n"
    layer2 = _b64(real)
    formatted = "\t" + layer2 + "\t"
    payload = _b64(formatted)
    normalized = _normalize_content(payload)
    broker = WorkspaceBroker()
    patch = {"op": "write", "path": "b.txt", "content": normalized}
    res = broker.apply_patch_set(str(tmp_path), [patch], Role.IMPLEMENTER, LEAD)
    assert res.errors == []
    assert (tmp_path / "b.txt").read_text() == real


def test_formatted_deep_nesting_rejected():
    # Six formatted layers exceed the effective decode depth: fail-closed
    # (the whitespace-padded inner layer must not dodge the depth cap).
    real = "six formatted layers"
    payload = real
    for _ in range(6):
        payload = _b64("  " + payload + "  ")
    with pytest.raises(ValueError, match="depth cap"):
        _normalize_content(payload)
