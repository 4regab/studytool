"""Link questions to answers. OWNER: agent B. Deterministic, no AI."""
from studytool.schemas import AnswerKeyEntry, SourceItem


def link_answers(items: list[SourceItem], key_entries: list[AnswerKeyEntry]) -> list[SourceItem]:
    """Set resolved_answer, answer_source and evidence on each question item.

    - Index key entries by (section_key, normalized question_number).
    - Precedence: inline answer / bold option, then the answer key, then explanation.
      Two sources that disagree -> answer_source='conflict', issue_reason set.
    - More than one key entry for the same (section, number) with different answers,
      or a number that matches several sections -> flag as ambiguous, do not guess.
    - Question with no answer anywhere -> answer_source='none', issue_reason set.
    - info_box items need no answer.
    Returns new items (inputs are not mutated).
    """
    raise NotImplementedError
