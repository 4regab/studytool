"""Deterministic card checks and duplicate detection. OWNER: agent C."""
from studytool.schemas import CardDraft, SourceItem


def check_card(card: CardDraft, item: SourceItem) -> list[str]:
    """Return flags, e.g. 'blank_field', 'answer_code_leak' (back is just 'BAC'/'(c)'/'A-3 B-1'),
    'source_dependent_wording' ('List-I', 'List II', 'Column A', 'the above', 'option'),
    'not_in_source' (key terms of the back missing from item text/evidence),
    'too_long'. Empty list = clean.
    """
    raise NotImplementedError


def fingerprint(front: str, back: str) -> str:
    """Stable hash of case/space/punctuation-normalized front+back, for duplicate detection."""
    raise NotImplementedError
