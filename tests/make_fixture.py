"""Generate fixtures/synthetic_exam.pdf: a small, controlled exam PDF used by tests.

Run directly: `uv run python tests/make_fixture.py` (or via pytest, which imports and
calls build()). No network, no git. Written straight to the fixtures/ folder.
"""
from pathlib import Path

import pymupdf

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
OUT_PATH = FIXTURES_DIR / "synthetic_exam.pdf"

PAGE_RECT = pymupdf.paper_rect("a4")

# The correct decoding for the Section A Q1 matching question:
#   Hastinapur  -> B.B. Lal (1950-52)
#   Kalibangan  -> B.K. Thapar
#   Lothal      -> S.R. Rao
#   Dholavira   -> R.S. Bisht
# List-II is printed scrambled (not in the same order as List-I), and the coded
# option (c) below is the one that decodes to the pairing above.


def _htmlbox(page: pymupdf.Page, rect, html: str) -> None:
    spare, scale = page.insert_htmlbox(rect, html, css="* {font-family: Helvetica; font-size: 11px;}")
    if spare == -1:
        raise RuntimeError(f"content did not fit in {rect}: {html[:80]!r}")


def build() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()

    # ---------------- Page 1 ----------------
    page1 = doc.new_page(width=PAGE_RECT.width, height=PAGE_RECT.height)
    y = 40
    _htmlbox(page1, pymupdf.Rect(40, y, 550, y + 40), "<b>Information Box</b>")
    y += 40
    _htmlbox(
        page1,
        pymupdf.Rect(40, y, 550, y + 60),
        "The Regulating Act of 1773 provided for the establishment of a Supreme Court at Calcutta. "
        "It also made the Governor of Bengal the Governor-General of British India.",
    )
    y += 70

    _htmlbox(page1, pymupdf.Rect(40, y, 550, y + 20), "<b>Section A</b>")
    y += 25
    _htmlbox(
        page1,
        pymupdf.Rect(40, y, 550, y + 20),
        "Q.1 Match List-I (Site) with List-II (Excavator) and select the correct answer using the codes given below:",
    )
    y += 30

    # List-I printed in order; List-II printed SCRAMBLED relative to the correct pairing.
    _htmlbox(
        page1,
        pymupdf.Rect(40, y, 290, y + 110),
        "<b>List-I (Site)</b><br>"
        "A. Hastinapur<br>"
        "B. Kalibangan<br>"
        "C. Lothal<br>"
        "D. Dholavira",
    )
    _htmlbox(
        page1,
        pymupdf.Rect(300, y, 550, y + 110),
        "<b>List-II (Excavator)</b><br>"
        "1. S.R. Rao<br>"
        "2. R.S. Bisht<br>"
        "3. B.B. Lal (1950-52)<br>"
        "4. B.K. Thapar",
    )
    y += 120

    # Correct decoding: A-3 (Hastinapur-B.B.Lal), B-4 (Kalibangan-B.K.Thapar),
    # C-1 (Lothal-S.R.Rao), D-2 (Dholavira-R.S.Bisht) -> option (c).
    _htmlbox(
        page1,
        pymupdf.Rect(40, y, 550, y + 70),
        "    A &nbsp; B &nbsp; C &nbsp; D<br>"
        "(a) 1 &nbsp; 2 &nbsp; 3 &nbsp; 4<br>"
        "(b) 2 &nbsp; 1 &nbsp; 4 &nbsp; 3<br>"
        "<b>(c) 3 &nbsp; 4 &nbsp; 1 &nbsp; 2</b><br>"
        "(d) 4 &nbsp; 3 &nbsp; 2 &nbsp; 1",
    )
    y += 80

    # Section A Q2 starts near the bottom of page 1 and continues onto page 2.
    _htmlbox(
        page1,
        pymupdf.Rect(40, y, 550, y + 20),
        "Q.2 Arrange the following events in chronological order:",
    )
    y += 25
    _htmlbox(
        page1,
        pymupdf.Rect(40, y, 550, y + 70),
        "1. Battle of Plassey<br>"
        "2. Battle of Buxar<br>"
        "3. Regulating Act, 1773<br>"
        "4. Pitt's India Act, 1784",
    )

    # ---------------- Page 2 ----------------
    page2 = doc.new_page(width=PAGE_RECT.width, height=PAGE_RECT.height)
    y = 40
    _htmlbox(
        page2,
        pymupdf.Rect(40, y, 550, y + 20),
        "(continuation of Q.2) Select the correct order using the codes below:",
    )
    y += 30
    # Correct order is 1,2,3,4 as printed above -> option (b) below is correct.
    _htmlbox(
        page2,
        pymupdf.Rect(40, y, 550, y + 60),
        "(a) 2-1-3-4<br>"
        "<b>(b) 1-2-3-4</b><br>"
        "(c) 1-3-2-4<br>"
        "(d) 4-3-2-1",
    )
    y += 70

    _htmlbox(
        page2,
        pymupdf.Rect(40, y, 550, y + 50),
        "Q.3 The Pitt's India Act was passed in which year?<br>"
        "(a) 1773<br>"
        "<b>(b) 1784</b><br>"
        "(c) 1793<br>"
        "(d) 1858",
    )
    y += 60

    _htmlbox(page2, pymupdf.Rect(40, y, 550, y + 20), "<b>Section B</b>")
    y += 25
    _htmlbox(
        page2,
        pymupdf.Rect(40, y, 550, y + 50),
        "Q.1 Who was the first Governor-General of independent India?<br>"
        "(a) Lord Mountbatten<br>"
        "(b) Jawaharlal Nehru<br>"
        "<b>(c) C. Rajagopalachari</b><br>"
        "(d) Lord Canning",
    )

    # ---------------- Page 3: Answer Key ----------------
    page3 = doc.new_page(width=PAGE_RECT.width, height=PAGE_RECT.height)
    y = 40
    _htmlbox(page3, pymupdf.Rect(40, y, 550, y + 20), "<b>Answer Key</b>")
    y += 30
    _htmlbox(page3, pymupdf.Rect(40, y, 550, y + 20), "<b>Section A</b>")
    y += 20
    _htmlbox(
        page3,
        pymupdf.Rect(40, y, 550, y + 60),
        "1-(c)<br>2-(b)<br>3-(b)",
    )
    y += 70
    _htmlbox(
        page3,
        pymupdf.Rect(40, y, 550, y + 20),
        "Explanation (A Q1): Hastinapur-B.B. Lal (1950-52), Kalibangan-B.K. Thapar, Lothal-S.R. Rao, Dholavira-R.S. Bisht.",
    )
    y += 40
    _htmlbox(page3, pymupdf.Rect(40, y, 550, y + 20), "<b>Section B</b>")
    y += 20
    _htmlbox(page3, pymupdf.Rect(40, y, 550, y + 20), "1-(c)")

    doc.save(OUT_PATH)
    doc.close()
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"wrote {path}")
