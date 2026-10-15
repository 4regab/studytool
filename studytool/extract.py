"""Page text and images with PyMuPDF. OWNER: agent B. Pure functions, no DB."""
import re
from pathlib import Path

import pymupdf

from studytool.schemas import Block, PageExtract

# A span counts as bold if its font flags include the bold bit, or its font
# name contains one of these (PDFs name "bold-ish" weights inconsistently).
_BOLD_NAME_HINTS = ("Bold", "Medi", "Black", "Semibold")


def _is_bold_span(span: dict) -> bool:
    if span["flags"] & 16:  # bit 16 (0x10) is the "bold" flag in PyMuPDF span flags
        return True
    font = span.get("font", "")
    return any(hint in font for hint in _BOLD_NAME_HINTS)


def _block_text_and_bold(block: dict) -> tuple[str, bool]:
    """Reading-order text for one block, with bold runs wrapped as **...**.

    Returns (text, all_bold) where all_bold is True only if every non-space
    span in the block is bold.
    """
    lines_out = []
    any_span = False
    all_bold = True
    for line in block.get("lines", []):
        line_parts = []
        for span in line.get("spans", []):
            text = span.get("text", "")
            if text == "":
                continue
            is_bold = _is_bold_span(span)
            if text.strip():
                any_span = True
                if not is_bold:
                    all_bold = False
            if is_bold and text.strip():
                line_parts.append(f"**{text}**")
            else:
                line_parts.append(text)
        lines_out.append("".join(line_parts))
    text = "\n".join(lines_out)
    return text, (all_bold and any_span)


def extract_page(pdf_path: Path, page_number: int) -> PageExtract:
    """Text blocks with coordinates, in reading order (sort=True).

    Bold spans (font flag bit 16, or 'Bold'/'Medi' in the font name) are wrapped as
    **...** in PageExtract.text. Set needs_vision when the page has little or no text
    or the text looks scrambled (e.g. mostly single-character lines).
    """
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_number - 1]
        raw_label = page.get_label()
        printed_label = raw_label if raw_label else None

        data = page.get_text("dict", sort=True)
        blocks: list[Block] = []
        text_parts: list[str] = []
        for raw_block in data.get("blocks", []):
            if "lines" not in raw_block:  # image block, not text
                continue
            block_text, block_bold = _block_text_and_bold(raw_block)
            if block_text.strip() == "":
                continue
            blocks.append(Block(text=block_text, bbox=tuple(raw_block["bbox"]), bold=block_bold))
            text_parts.append(block_text)

        full_text = "\n".join(text_parts)
        needs_vision = _needs_vision(full_text)

        return PageExtract(
            page_number=page_number,
            printed_label=printed_label,
            text=full_text,
            blocks=blocks,
            needs_vision=needs_vision,
        )
    finally:
        doc.close()


def _needs_vision(text: str) -> bool:
    """Heuristic: too little text, or text that looks scrambled into single chars."""
    non_space = re.sub(r"\s", "", text)
    if len(non_space) < 40:
        return True

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    one_char_lines = sum(1 for ln in lines if len(ln.strip()) <= 1)
    return (one_char_lines / len(lines)) > 0.5


def render_page(pdf_path: Path, page_number: int, out_dir: Path, zoom: float = 2.0) -> Path:
    """Render a page to PNG once and reuse the file on later calls."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{pdf_path.stem}_p{page_number}_z{zoom}.png"
    if out_path.exists():
        return out_path

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_number - 1]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        pix.save(out_path)
    finally:
        doc.close()

    return out_path
