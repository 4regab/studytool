"""Page text and images with PyMuPDF. OWNER: agent B. Pure functions, no DB."""
from pathlib import Path

from studytool.schemas import PageExtract


def extract_page(pdf_path: Path, page_number: int) -> PageExtract:
    """Text blocks with coordinates, in reading order (sort=True).

    Bold spans (font flag bit 16, or 'Bold'/'Medi' in the font name) are wrapped as
    **...** in PageExtract.text. Set needs_vision when the page has little or no text
    or the text looks scrambled (e.g. mostly single-character lines).
    """
    raise NotImplementedError


def render_page(pdf_path: Path, page_number: int, out_dir: Path, zoom: float = 2.0) -> Path:
    """Render a page to PNG once and reuse the file on later calls."""
    raise NotImplementedError
