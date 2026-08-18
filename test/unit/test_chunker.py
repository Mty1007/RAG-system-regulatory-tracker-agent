"""Unit tests for core/chunker.py.

Focuses on:
1. Docling HTML comment + page-break noise stripping — the fix that restored
   mid-sentence regulatory figures (e.g. "98%") to single coherent chunks.
2. Heading-split behaviour — sections stay under max_chars as one chunk.
3. Sliding-window split — sections over max_chars are split with overlap.
4. min_chars filter — very short fragments are discarded as noise.
5. chunk_id / chunk_index shape — downstream AstraDB store depends on these.
"""

from core.chunker import chunk_markdown


# ── 1. Docling noise stripping ────────────────────────────────────────────────

def test_html_comments_stripped_from_chunks():
    """HTML comment blocks injected by Docling must not appear in any chunk text."""
    md = (
        "# Client virtual assets\n\n"
        "The Platform Operator and its Associated Entity should store\n\n"
        "<!--\nDetected language: en\n-->\n\n"
        "---\n\n"
        "<!-- Page 63 -->\n\n"
        "<!-- image -->\n\n"
        "98% of client virtual assets in cold storage."
    )
    chunks = chunk_markdown("sfc-test", md)
    all_text = " ".join(c["text"] for c in chunks)
    assert "<!--" not in all_text, "HTML comment opener must be stripped"
    assert "-->" not in all_text, "HTML comment closer must be stripped"
    assert "Page 63" not in all_text, "Page marker content must be stripped"
    assert "Detected language" not in all_text, "Language marker must be stripped"


def test_98_percent_stays_in_single_chunk():
    """The '98%' figure must survive in the same chunk as 'cold storage'.

    This is the exact regression this fix was written for — Docling page-break
    comments were splitting the sentence across chunk boundaries so the figure
    was stranded in a low-scoring fragment that the reranker never surfaced.
    """
    md = (
        "# Client virtual assets\n\n"
        "The operator should not deal with client assets except for settlement.\n\n"
        "<!--\nDetected language: en\n-->\n\n"
        "---\n\n"
        "<!-- Page 63 -->\n\n"
        "<!-- image -->\n\n"
        "The Platform Operator and its Associated Entity should store "
        "98% of client virtual assets in cold storage (such as HSM-based "
        "cold storage) except under limited circumstances permitted by the SFC."
    )
    chunks = chunk_markdown("sfc-test", md)
    hits = [c for c in chunks if "98%" in c["text"] and "cold storage" in c["text"]]
    assert len(hits) >= 1, (
        "Expected at least one chunk containing both '98%' and 'cold storage' "
        "after HTML comment stripping — sentence was split across chunk boundaries"
    )


def test_page_hr_dividers_stripped():
    """Standalone --- dividers (Docling page-break artefacts) must not appear in chunks."""
    md = (
        "# Section\n\n"
        "First sentence of the section.\n\n"
        "---\n\n"
        "Continuation after page break."
    )
    chunks = chunk_markdown("sfc-test", md)
    for c in chunks:
        assert "\n---\n" not in c["text"], (
            f"Page-break divider leaked into chunk: {c['text'][:80]!r}"
        )


def test_barcode_comments_stripped():
    """Barcode/QR-code comment blocks (PCPD docs) must be stripped."""
    md = (
        "# Overview\n\n"
        "Important data protection guidance.\n\n"
        "<!--\nBarcode format: QRCode\n"
        "Barcode value: https://www.pcpd.org.hk/\n-->\n\n"
        "More guidance follows."
    )
    chunks = chunk_markdown("pcpd-test", md)
    all_text = " ".join(c["text"] for c in chunks)
    assert "Barcode" not in all_text
    assert "QRCode" not in all_text
    assert "pcpd.org.hk" not in all_text


# ── 2. Heading split ──────────────────────────────────────────────────────────

def test_section_under_max_chars_is_single_chunk():
    """A section whose body fits within max_chars must produce exactly one chunk."""
    # Body must be >= min_chars (80) and <= max_chars (480) to produce exactly 1 chunk
    body = "A licensed corporation must segregate client assets from its own assets. " * 2
    md = f"# Requirements\n\n{body}"
    chunks = chunk_markdown("sfc-test", md)
    assert len(chunks) == 1
    assert chunks[0]["section_heading"] == "Requirements"
    assert "segregate client assets" in chunks[0]["text"]


def test_heading_propagates_to_all_windows():
    """When a section is large enough to be split into windows, every window
    must carry the section heading."""
    body = "word " * 200          # well over DEFAULT_MAX_CHARS (480)
    md = f"# Data Retention\n\n{body}"
    chunks = chunk_markdown("pcpd-test", md)
    assert len(chunks) > 1, "Long section should produce multiple chunks"
    for c in chunks:
        assert c["section_heading"] == "Data Retention", (
            f"Heading missing on chunk {c['chunk_index']}"
        )


# ── 3. Chunk shape ────────────────────────────────────────────────────────────

def test_chunk_ids_are_unique_and_sequential():
    """chunk_id must be unique per chunk and chunk_index must be 0-based sequential."""
    md = (
        "# Section A\n\nFirst section body text here.\n\n"
        "# Section B\n\nSecond section body text here."
    )
    chunks = chunk_markdown("sfc-abc123", md)
    ids     = [c["chunk_id"] for c in chunks]
    indices = [c["chunk_index"] for c in chunks]
    assert len(ids) == len(set(ids)), "chunk_ids must be unique"
    assert indices == list(range(len(chunks))), "chunk_index must be 0-based sequential"
    for c in chunks:
        assert c["chunk_id"] == f"sfc-abc123__c{c['chunk_index']:04d}"


def test_min_chars_discards_noise_fragments():
    """Chunks shorter than min_chars (default 80) must be discarded."""
    md = "# Title\n\nToo short.\n\n# Real Section\n\n" + "regulatory content " * 10
    chunks = chunk_markdown("sfc-test", md)
    for c in chunks:
        assert len(c["text"]) >= 80, (
            f"Chunk shorter than min_chars survived: {c['text']!r}"
        )


def test_empty_markdown_returns_no_chunks():
    """Empty or whitespace-only markdown must return an empty list."""
    assert chunk_markdown("sfc-test", "") == []
    assert chunk_markdown("sfc-test", "   \n\n  ") == []
