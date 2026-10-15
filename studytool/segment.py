"""Split pages into typed source items and answer-key entries. OWNER: agent B.

Uses ai.call_json with the page text (and the page image when needs_vision).
Each call sees one page plus the next page's text as overlap, so items that run
across a page boundary are assembled whole. An item belongs to the page where it
starts; the overlap page only completes it.
"""
from pathlib import Path

from studytool.schemas import ModelUsage, PageExtract, SegmentResult


def segment_page(
    page: PageExtract,
    next_page: PageExtract | None,
    prev_section_key: str | None,
    is_key_page: bool,
    image_path: Path | None = None,
) -> tuple[SegmentResult, list[ModelUsage]]:
    """Items that START on `page`, plus answer-key entries found on it.

    prev_section_key carries the running section across pages so repeated question
    numbers in different sections stay distinct. If is_key_page, mark every item
    from_key_pages_only=True (they provide evidence, never cards).
    """
    raise NotImplementedError
