"""Deterministic card checks and duplicate detection. OWNER: agent C."""
import hashlib
import re

from studytool.schemas import CardDraft, ItemType, SourceItem

_CODE_PATTERNS = [
    re.compile(r"^\(?[a-dA-D]\)?\.?$"),                              # "(c)", "C", "c."
    re.compile(r"^[A-D]{2,}$"),                                      # "BAC", "ACD"
    re.compile(r"^[A-D]-?\d( ?,? ?[A-D]-?\d)+$", re.IGNORECASE),      # "A-3, B-1, C-4"
    re.compile(r"^\d( ?\d)+$"),                                      # "3 1 4 2"
]
_CODE_TYPES: set[ItemType] = {"matching", "chronology", "mcq"}

_SOURCE_DEPENDENT_PATTERNS = [
    re.compile(r"list[- ]?i{1,2}\b", re.IGNORECASE),
    re.compile(r"column [ab12]", re.IGNORECASE),
    re.compile(r"\boption [a-d]\b", re.IGNORECASE),
    re.compile(r"\bthe above\b", re.IGNORECASE),
    re.compile(r"\bcode given below\b", re.IGNORECASE),
    re.compile(r"\bselect the correct answer\b", re.IGNORECASE),
]

_STOPWORDS = {
    "that", "this", "with", "from", "which", "there", "their", "about",
    "would", "could", "should", "these", "those", "where", "when", "what",
    "have", "been", "were", "they", "them", "than", "then", "also", "into",
    "being", "both", "each", "only", "some", "such", "very", "most",
}

_MAX_FIELD_LEN = 300


def check_card(card: CardDraft, item: SourceItem) -> list[str]:
    """Return flags, e.g. 'blank_field', 'answer_code_leak' (back is just 'BAC'/'(c)'/'A-3 B-1'),
    'source_dependent_wording' ('List-I', 'List II', 'Column A', 'the above', 'option'),
    'not_in_source' (key terms of the back missing from item text/evidence),
    'too_long'. Empty list = clean.
    """
    flags: list[str] = []
    front = card.front or ""
    back = card.back or ""

    if not front.strip() or not back.strip():
        flags.append("blank_field")

    if item.type in _CODE_TYPES:
        back_stripped = back.strip()
        if any(p.match(back_stripped) for p in _CODE_PATTERNS):
            flags.append("answer_code_leak")

    combined = f"{front}\n{back}"
    if any(p.search(combined) for p in _SOURCE_DEPENDENT_PATTERNS):
        flags.append("source_dependent_wording")

    if len(front) > _MAX_FIELD_LEN or len(back) > _MAX_FIELD_LEN:
        flags.append("too_long")

    if back.strip() and _not_in_source(back, item):
        flags.append("not_in_source")

    return flags


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z]{4,}|\d+", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _not_in_source(back: str, item: SourceItem) -> bool:
    """Lenient check: most content words of the back should appear somewhere in the source text."""
    back_words = _content_words(back)
    if not back_words:
        return False
    source_text = " ".join(
        filter(None, [item.raw_content, item.explanation, *(e.excerpt for e in item.evidence)])
    ).lower()
    missing = [w for w in back_words if w not in source_text]
    # Lenient: flag only if most of the content words are missing.
    return len(missing) > len(back_words) / 2


_FINGERPRINT_STRIP_RE = re.compile(r"[^\w\s]")
_FINGERPRINT_SPACE_RE = re.compile(r"\s+")


def fingerprint(front: str, back: str) -> str:
    """Stable hash of case/space/punctuation-normalized front+back, for duplicate detection."""
    def norm(s: str) -> str:
        s = s.lower()
        s = _FINGERPRINT_STRIP_RE.sub("", s)
        s = _FINGERPRINT_SPACE_RE.sub(" ", s).strip()
        return s

    combined = norm(front) + "|" + norm(back)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()
