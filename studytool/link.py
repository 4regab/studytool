"""Link questions to answers. OWNER: agent B. Deterministic, no AI."""
import re

from studytool.schemas import AnswerKeyEntry, EvidenceIn, SourceItem

_QNUM_STRIP_RE = re.compile(r"[Q.()\s]", re.IGNORECASE)
_ANSWER_STRIP_RE = re.compile(r"[().\s]")


def _normalize_question_number(raw: str | None) -> str | None:
    """'Q.12', '12.', '(12)' -> '12'."""
    if raw is None:
        return None
    cleaned = _QNUM_STRIP_RE.sub("", raw)
    return cleaned or None


def _normalize_answer_label(raw: str | None) -> str | None:
    """'(c)', 'c)', 'C' -> 'c', compared case-insensitively. A mark with no letter or digit,
    like '(*)' (books use it when no single option is correct), is not an answer."""
    if raw is None:
        return None
    cleaned = _ANSWER_STRIP_RE.sub("", raw)
    if not any(ch.isalnum() for ch in cleaned):
        return None
    return cleaned.lower() or None


def _resolve_label_case(item: SourceItem, normalized_label: str) -> str:
    """Return the label spelled as the item's own options print it, if a match exists."""
    for option in item.options:
        if _normalize_answer_label(option.label) == normalized_label:
            return option.label
    return normalized_label


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
    items = [item.model_copy(deep=True) for item in items]

    # Index key entries by (section_key, normalized question_number).
    key_index: dict[tuple[str, str], list[AnswerKeyEntry]] = {}
    for entry in key_entries:
        norm_number = _normalize_question_number(entry.question_number)
        if norm_number is None:
            continue
        key_index.setdefault((entry.section_key, norm_number), []).append(entry)

    # A bare question number (ignoring section) that matches more than one section is ambiguous
    # only when the item's own section has no exact match - section_key already disambiguates
    # repeated numbers across sections, so we track how many distinct sections use this number
    # purely to flag true cross-section ambiguity when an item's section is unknown/blank.
    number_to_sections: dict[str, set[str]] = {}
    for (section_key, norm_number) in key_index:
        number_to_sections.setdefault(norm_number, set()).add(section_key)

    for item in items:
        if item.type == "info_box":
            continue

        norm_number = _normalize_question_number(item.question_number)

        inline_label = _normalize_answer_label(item.inline_answer)
        # A bold option also counts as an inline answer even if inline_answer wasn't set.
        if inline_label is None:
            for option in item.options:
                if option.bold:
                    inline_label = _normalize_answer_label(option.label)
                    break

        key_matches: list[AnswerKeyEntry] = []
        ambiguous_section = False
        if norm_number is not None:
            key_matches = key_index.get((item.section_key, norm_number), [])
            if not key_matches:
                # No match in this item's own section. Check if the number is ambiguous
                # across other sections (informational only - we never guess across sections).
                sections_with_number = number_to_sections.get(norm_number, set())
                if len(sections_with_number) > 1 and item.section_key not in sections_with_number:
                    ambiguous_section = True

        key_label = None
        key_explanation = None
        key_is_ambiguous = False
        if len(key_matches) == 1:
            key_label = _normalize_answer_label(key_matches[0].answer)
            key_explanation = key_matches[0].explanation
        elif len(key_matches) > 1:
            distinct_answers = {_normalize_answer_label(entry.answer) for entry in key_matches}
            if len(distinct_answers) > 1:
                key_is_ambiguous = True
            else:
                key_label = next(iter(distinct_answers))
                key_explanation = next((entry.explanation for entry in key_matches if entry.explanation), None)

        explanation_label = None  # the model may have put an inline "Answer: X" inside explanation text; not parsed here.

        resolved_label = None
        answer_source = "none"
        issue_reason = None

        if key_is_ambiguous:
            issue_reason = f"Multiple answer-key entries for question {item.question_number!r} disagree."
        elif ambiguous_section and inline_label is None:
            issue_reason = f"Question number {item.question_number!r} matches more than one section; cannot resolve without a section match."
        elif inline_label is not None and key_label is not None:
            if inline_label == key_label:
                resolved_label = inline_label
                answer_source = "bold_marker" if item.inline_answer is None else "inline"
            else:
                answer_source = "conflict"
                issue_reason = (
                    f"Inline answer {inline_label!r} conflicts with answer key {key_label!r} "
                    f"for question {item.question_number!r}."
                )
        elif inline_label is not None:
            resolved_label = inline_label
            answer_source = "bold_marker" if item.inline_answer is None else "inline"
        elif key_label is not None:
            resolved_label = key_label
            answer_source = "answer_key"
        elif explanation_label is not None:
            resolved_label = explanation_label
            answer_source = "explanation"
        else:
            answer_source = "none"
            if item.inline_answer:
                issue_reason = f"The source prints the answer as {item.inline_answer!r}, not a single option."
            else:
                issue_reason = f"No answer found for question {item.question_number!r} in section {item.section_key!r}."

        if resolved_label is not None:
            item.resolved_answer = _resolve_label_case(item, resolved_label)
        item.answer_source = answer_source
        if issue_reason is not None:
            item.issue_reason = issue_reason

        # Evidence from the sources actually used.
        if inline_label is not None:
            kind = "bold_marker" if item.inline_answer is None else "inline_answer"
            item.evidence.append(
                EvidenceIn(
                    page_number=item.page_refs[0] if item.page_refs else 0,
                    excerpt=f"inline answer: {item.inline_answer or inline_label}",
                    kind=kind,
                )
            )
        for entry in key_matches:
            item.evidence.append(
                EvidenceIn(
                    page_number=entry.page_number,
                    excerpt=entry.excerpt,
                    kind="answer_key",
                )
            )
            if entry.explanation:
                item.evidence.append(
                    EvidenceIn(
                        page_number=entry.page_number,
                        excerpt=entry.explanation,
                        kind="explanation",
                    )
                )

        # Explanations from key entries copy into item.explanation.
        if key_explanation and not item.explanation:
            item.explanation = key_explanation

    return items
