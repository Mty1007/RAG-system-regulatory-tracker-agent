"""Unit tests for cross-reference chunk filter in core/generator.py.

Tests cover:
1. _is_crossref_only() — pure function, no API calls needed.
2. _filter_crossref_chunks() — list-level filter, pure, no API calls.

These functions were added by the mentor in commit 3b05540 to prevent
short "please refer to" chunks from occupying top-3 context slots and
degrading faithfulness scoring.
"""

from core.generator import _filter_crossref_chunks, _is_crossref_only


# ── _is_crossref_only ─────────────────────────────────────────────────────────

def test_short_all_xref_returns_true():
    """A short chunk consisting entirely of cross-reference sentences is filtered."""
    text = "Please refer to the Circular on virtual asset trading platforms."
    assert _is_crossref_only(text) is True


def test_short_mixed_xref_phrases_returns_true():
    """Multiple recognised xref phrases together must trigger the filter."""
    text = (
        "For details see the guideline on client assets. "
        "Refer to the circular issued in January 2024."
    )
    assert _is_crossref_only(text) is True


def test_long_chunk_never_filtered():
    """Chunks >= 300 chars must never be filtered regardless of content."""
    # Build a chunk that is >= 300 chars and is all xref text
    text = "Please refer to the Circular. " * 15   # well over 300 chars
    assert len(text) >= 300
    assert _is_crossref_only(text) is False


def test_substantive_short_chunk_not_filtered():
    """A short chunk with real rule text must not be filtered."""
    text = (
        "A licensed corporation must segregate client assets from its own "
        "assets at all times."
    )
    assert _is_crossref_only(text) is False


def test_mixed_rule_and_xref_not_filtered():
    """A chunk that contains real rule text alongside a reference must survive.

    The filter only fires when >= 80% of sentences are xref-only.  A chunk
    with two substantive sentences and one xref sentence (33%) must not fire.
    """
    text = (
        "A Platform Operator must store 98% of client virtual assets in cold storage. "
        "Cold storage includes HSM-based hardware security modules. "
        "For further details see the SFC circular on virtual assets."
    )
    assert _is_crossref_only(text) is False


def test_empty_text_returns_false():
    """Empty or whitespace-only text must not be filtered (no sentences)."""
    assert _is_crossref_only("") is False
    assert _is_crossref_only("   ") is False


def test_xref_re_case_insensitive():
    """Pattern match must be case-insensitive (documents vary capitalisation)."""
    text = "PLEASE REFER TO the Circular on licensing requirements."
    assert _is_crossref_only(text) is True


# ── _filter_crossref_chunks ───────────────────────────────────────────────────

def _make_chunk(text: str, doc_id: str = "sfc-test") -> dict:
    return {
        "doc_id": doc_id,
        "chunk_id": f"{doc_id}__c0000",
        "chunk_index": 0,
        "section_heading": "Test Section",
        "page_start": 1,
        "text": text,
        "token_count": len(text.split()),
    }


def test_xref_chunk_removed_when_enough_remain():
    """A cross-reference-only chunk is removed when >= 3 substantive chunks exist."""
    substantive = "A licensed corporation must maintain adequate liquid capital. " * 3
    xref = "Please refer to the Circular on capital requirements."

    chunks = [_make_chunk(substantive)] * 4 + [_make_chunk(xref)]
    result = _filter_crossref_chunks(chunks)

    assert len(result) == 4
    for c in result:
        assert "Please refer to" not in c["text"]


def test_fallback_keeps_original_when_too_few_remain():
    """If filtering would leave fewer than 3 chunks, return the original list unchanged."""
    substantive = "A licensed corporation must segregate client assets at all times."
    xref = "Please refer to the Circular. For details see the guideline."

    # Only 2 substantive + 1 xref — filtering would leave 2, below the floor
    chunks = [_make_chunk(substantive)] * 2 + [_make_chunk(xref)]
    result = _filter_crossref_chunks(chunks)

    # Must fall back to original — all 3 chunks returned unchanged
    assert result == chunks


def test_no_xref_chunks_unchanged():
    """A list with no cross-reference chunks must be returned unchanged."""
    substantive = "Client assets must be segregated from firm assets at all times. " * 2
    chunks = [_make_chunk(substantive)] * 5
    result = _filter_crossref_chunks(chunks)
    assert result == chunks
    assert len(result) == 5
