"""Data shapes shared by every stage. This file is the contract between modules.

Page numbers are 1-based everywhere. Bold text in page text is wrapped as **...**
because some PDFs mark the correct option only by bold type.
"""
from typing import Literal

from pydantic import BaseModel, Field

ItemType = Literal[
    "info_box",          # explanatory text / fact box
    "mcq",               # ordinary single-answer multiple choice
    "matching",          # List I / List II, Column A / B, or an unlabeled pairing table
    "chronology",        # arrange events in order
    "statement",         # "Which of the statements is/are correct"
    "assertion_reason",  # Assertion (A) / Reason (R)
    "unsupported",       # detected but not a supported type; gets a skip reason
]
EvidenceKind = Literal["question", "answer_key", "explanation", "fact", "bold_marker", "inline_answer"]
AnswerSource = Literal["inline", "bold_marker", "answer_key", "explanation", "none", "conflict"]
Disposition = Literal["pending", "converted", "duplicate", "blocked", "skipped"]


# ---------- extraction ----------

class Block(BaseModel):
    text: str
    bbox: tuple[float, float, float, float]
    bold: bool = False      # True if every non-space span in the block is bold


class PageExtract(BaseModel):
    page_number: int
    printed_label: str | None = None
    text: str                 # reading-order text, bold spans as **...**
    blocks: list[Block] = []
    needs_vision: bool = False  # text missing/scrambled; segmentation should use the page image


# ---------- segmentation and linking ----------

class Option(BaseModel):
    label: str   # "A", "b", "1", "(c)" as printed
    text: str
    bold: bool = False


class EvidenceIn(BaseModel):
    page_number: int
    excerpt: str
    bbox: tuple[float, float, float, float] | None = None
    kind: EvidenceKind


class SourceItem(BaseModel):
    """One detected unit of study content.

    item_key must be stable across reruns of the same pages and settings:
    f"{section_key}|{question_number or 'u'+str(index_on_first_page)}|p{first_page}".
    """
    item_key: str
    section_key: str = "main"        # chapter/section/test id; question numbers repeat across sections
    question_number: str | None = None
    type: ItemType
    page_refs: list[int]
    raw_content: str                 # verbatim source text of the item (stem + options + tables)
    stem: str | None = None
    options: list[Option] = []
    list_i_title: str | None = None   # e.g. "Site"; the column's meaning, not just "List-I"
    list_ii_title: str | None = None  # e.g. "Excavator"
    list_i: list[Option] = []
    list_ii: list[Option] = []
    statements: list[Option] = []     # numbered statements, or [Assertion, Reason]
    events: list[Option] = []         # chronology items to order
    inline_answer: str | None = None  # answer printed with the question ("Answer: B", bold option)
    explanation: str | None = None
    resolved_answer: str | None = None   # set by link stage, e.g. "c" or "B"
    answer_source: AnswerSource = "none"
    evidence: list[EvidenceIn] = []
    from_key_pages_only: bool = False  # lies only on answer-key pages: evidence, never cards
    disposition: Disposition = "pending"
    issue_reason: str | None = None


class AnswerKeyEntry(BaseModel):
    section_key: str = "main"
    question_number: str
    answer: str                       # option label as printed, e.g. "c", "(B)"
    explanation: str | None = None
    page_number: int
    excerpt: str


class SegmentResult(BaseModel):
    items: list[SourceItem] = []
    key_entries: list[AnswerKeyEntry] = []


# ---------- normalization (deterministic decoding of the answer) ----------

class Relationship(BaseModel):
    left: str                     # actual entity name, e.g. "Hastinapur"
    right: str                    # e.g. "B.B. Lal (1950–52)"
    left_meaning: str | None = None   # column meaning, e.g. "archaeological site"
    right_meaning: str | None = None  # e.g. "excavator"


class StatementTruth(BaseModel):
    text: str
    truth: Literal["true", "false", "unknown"]


class Normalized(BaseModel):
    type: ItemType
    question: str | None = None
    correct_answer_text: str | None = None          # mcq: text of the correct option, no letter
    facts: list[str] = []                           # info_box facts
    relationships: list[Relationship] = []          # matching: verified pairs only
    rejected_relationships: list[Relationship] = [] # "incorrectly matched" pairs (false facts)
    ordered_events: list[str] = []                  # chronology: event names in correct order
    statements: list[StatementTruth] = []
    reason_explains_assertion: bool | None = None   # assertion_reason: None = not supported
    issues: list[str] = []                          # anything that blocks automatic conversion


# ---------- generation ----------

class CardDraft(BaseModel):
    front: str
    back: str
    evidence_indexes: list[int] = []   # indexes into SourceItem.evidence
    flags: list[str] = []              # validation flags (see validate.py)
    review_reason: str | None = None   # non-None -> card stored as needs_review


class ModelUsage(BaseModel):
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None


class GenerationResult(BaseModel):
    normalized: Normalized
    cards: list[CardDraft] = []
    disposition: Disposition          # converted / blocked / skipped
    issue_reason: str | None = None
    usage: list[ModelUsage] = []
