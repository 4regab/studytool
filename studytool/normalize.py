"""Turn a linked SourceItem into Normalized facts. OWNER: agent C. Deterministic, no AI.

This is where answer codes are decoded, so it must never guess:
- matching: parse the chosen option's code ("A-3, B-1, C-4, D-2", "A-iii B-i ...",
  "3 1 4 2") into pairs of actual entity names. Never assume adjacent rows match.
  "Which pair is incorrectly matched": that pair goes to rejected_relationships;
  the others are true only if the question states only one pair is wrong.
- chronology: decode "B A C" / "2-1-3" into ordered event names. No dates invented.
- mcq: correct_answer_text = text of the resolved option, letter removed.
- statement / assertion_reason: truth per statement only from the decoded answer.
Anything that cannot be decoded goes into Normalized.issues.
"""
from studytool.schemas import Normalized, SourceItem


def normalize(item: SourceItem) -> Normalized:
    raise NotImplementedError
