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
import re

from studytool.schemas import Normalized, Option, Relationship, SourceItem, StatementTruth

ROMAN_MAP = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10, "xi": 11, "xii": 12,
}


def _label_key(label: str) -> str:
    """Normalize a printed label ('(c)', 'C.', ' c ') down to its bare form, lowercased."""
    return re.sub(r"[().\s]", "", label).lower()


def _ordinal_of(token: str) -> int | None:
    """Turn a letter (A->1), a number ('3'->3), or a roman numeral ('iii'->3) into a 1-based index."""
    t = token.strip().lower()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if t in ROMAN_MAP:
        return ROMAN_MAP[t]
    if len(t) == 1 and t.isalpha():
        return ord(t) - ord("a") + 1
    return None


def _find_option(item: SourceItem, label: str) -> Option | None:
    key = _label_key(label)
    for opt in item.options:
        if _label_key(opt.label) == key:
            return opt
    return None


def _by_ordinal(entries: list[Option]) -> dict[int, Option]:
    """Map each entry's own 1-based position AND its printed label's ordinal to itself.

    List rows are usually printed in order (A, B, C.. or 1, 2, 3..), so position doubles
    as a decode target; the printed label is kept as a second lookup path in case rows
    were presented out of order.
    """
    out: dict[int, Option] = {}
    for i, entry in enumerate(entries, start=1):
        out[i] = entry
        ordv = _ordinal_of(entry.label)
        if ordv is not None:
            out[ordv] = entry
    return out


# A pair token: "A-3", "A3", "A - 3", with letter/number/roman on either side.
_PAIR_RE = re.compile(r"([A-Za-z]+|\d+)\s*[-:]\s*([A-Za-z]+|\d+)")


def _parse_matching_code(code: str, list_i: list[Option], list_ii: list[Option]) -> list[Relationship] | None:
    """Decode a matching answer option's code text into (list_i entry, list_ii entry) pairs.

    Returns None if the code cannot be confidently decoded.
    """
    code = code.strip()
    i_by_ord = _by_ordinal(list_i)
    ii_by_ord = _by_ordinal(list_ii)

    pairs: list[tuple[int, int]] = []

    # Form "A-3, B-1, C-4, D-2" or "A3 B1" etc: explicit left-right tokens.
    found = _PAIR_RE.findall(code)
    if found and len(found) == len(list_i):
        ok = True
        for left_tok, right_tok in found:
            left_ord = _ordinal_of(left_tok)
            right_ord = _ordinal_of(right_tok)
            if left_ord is None or right_ord is None:
                ok = False
                break
            pairs.append((left_ord, right_ord))
        if ok:
            return _pairs_to_relationships(pairs, i_by_ord, ii_by_ord, list_i)

    # Grid form: two rows of labels, e.g. "A B C D / 3 1 4 2" or "A B C D\n3 1 4 2".
    rows = [r.strip() for r in re.split(r"[\n/]", code) if r.strip()]
    if len(rows) == 2:
        left_tokens = re.findall(r"[A-Za-z]+|\d+", rows[0])
        right_tokens = re.findall(r"[A-Za-z]+|\d+", rows[1])
        if len(left_tokens) == len(right_tokens) == len(list_i):
            ok = True
            for left_tok, right_tok in zip(left_tokens, right_tokens):
                left_ord = _ordinal_of(left_tok)
                right_ord = _ordinal_of(right_tok)
                if left_ord is None or right_ord is None:
                    ok = False
                    break
                pairs.append((left_ord, right_ord))
            if ok:
                return _pairs_to_relationships(pairs, i_by_ord, ii_by_ord, list_i)

    # Bare sequence of numbers: "3 1 4 2" means list_i[1]->3, list_i[2]->1, ... (position order).
    bare_nums = re.findall(r"\d+", code)
    if bare_nums and len(bare_nums) == len(list_i) and not re.search(r"[A-Za-z]", code):
        pairs = [(i, int(n)) for i, n in enumerate(bare_nums, start=1)]
        return _pairs_to_relationships(pairs, i_by_ord, ii_by_ord, list_i)

    return None


def _pairs_to_relationships(
    pairs: list[tuple[int, int]], i_by_ord: dict[int, Option], ii_by_ord: dict[int, Option], list_i: list[Option]
) -> list[Relationship] | None:
    rels = []
    for left_ord, right_ord in pairs:
        left = i_by_ord.get(left_ord)
        right = ii_by_ord.get(right_ord)
        if left is None or right is None:
            return None
        rels.append(Relationship(left=left.text, right=right.text))
    if len(rels) != len(list_i):
        return None
    return rels


def _parse_single_pair(text: str, list_i: list[Option], list_ii: list[Option]) -> Relationship | None:
    """Decode one option's text that names a single pair, either as a short code ("A-3")
    or as the actual entity text written out ("Hastinapur - B.K. Thapar (1951-52)")."""
    text = text.strip()

    # Try matching against actual entity texts first: find which list_i entry's text is a
    # prefix of the option text, then take the remainder (after a separator) as the list_ii side.
    for li in list_i:
        if text.lower().startswith(li.text.lower()):
            remainder = text[len(li.text):].strip()
            remainder = re.sub(r"^[-:\u2013\u2014]+\s*", "", remainder)
            if remainder:
                for rii in list_ii:
                    if rii.text.lower() == remainder.lower() or remainder.lower() in rii.text.lower():
                        return Relationship(left=li.text, right=rii.text)
                # remainder didn't match a known list_ii entry exactly; still usable as given
                return Relationship(left=li.text, right=remainder)

    # Fall back to a short code like "A-3".
    rels = _parse_matching_code(text, list_i, list_ii)
    if rels and len(rels) == 1:
        return rels[0]
    return None


_CHRONO_INCORRECT_RE = re.compile(r"\bnot correctly matched\b|\bincorrectly matched\b", re.IGNORECASE)
_SINGLE_WRONG_RE = re.compile(r"which\s+one\s+of\s+the\s+following\s+pairs?\s+is\s+not\s+correctly\s+matched", re.IGNORECASE)


def _normalize_matching(item: SourceItem, norm: Normalized) -> None:
    if not item.list_i or not item.list_ii:
        norm.issues.append("matching item missing list_i or list_ii entries")
        return
    opt = _find_option(item, item.resolved_answer or "")
    if opt is None:
        norm.issues.append(f"resolved answer '{item.resolved_answer}' does not match any printed option")
        return

    is_incorrect_question = bool(item.stem and _CHRONO_INCORRECT_RE.search(item.stem))

    if is_incorrect_question:
        # The selected option's text is a single wrong pair, either a short code ("A-3")
        # or the actual entity names written out ("Hastinapur - B.K. Thapar (1951-52)").
        wrong_pair = _parse_single_pair(opt.text, item.list_i, item.list_ii)
        if wrong_pair is None:
            norm.issues.append("could not decode the single incorrect pair from the chosen option")
            return
        wrong_pairs = [wrong_pair]
        norm.rejected_relationships = wrong_pairs
        # Only promote the remaining pairs to verified relationships if the stem makes it
        # explicit that exactly one pair is wrong (so the rest are, by elimination, correct).
        if item.stem and _SINGLE_WRONG_RE.search(item.stem):
            wrong_right_texts = {r.right for r in wrong_pairs}
            others = [o for o in item.list_ii if o.text not in wrong_right_texts]
            # Pair remaining list_i rows with remaining list_ii rows by their own position/label.
            wrong_left_texts = {r.left for r in wrong_pairs}
            remaining_left = [o for o in item.list_i if o.text not in wrong_left_texts]
            if len(remaining_left) == len(others):
                for l, r in zip(remaining_left, others):
                    norm.relationships.append(Relationship(left=l.text, right=r.text))
        return

    rels = _parse_matching_code(opt.text, item.list_i, item.list_ii)
    if rels is None:
        norm.issues.append(f"could not decode matching code '{opt.text}'")
        return
    for r in rels:
        r.left_meaning = item.list_i_title
        r.right_meaning = item.list_ii_title
    norm.relationships = rels


def _normalize_chronology(item: SourceItem, norm: Normalized) -> None:
    if not item.events:
        norm.issues.append("chronology item has no listed events")
        return
    opt = _find_option(item, item.resolved_answer or "")
    if opt is None:
        norm.issues.append(f"resolved answer '{item.resolved_answer}' does not match any printed option")
        return

    events_by_ord = _by_ordinal(item.events)
    code = opt.text.strip()

    # "B, A, C" / "B A C" (letters) or "2-1-3" / "2 1 3" (numbers).
    tokens = re.findall(r"[A-Za-z]+|\d+", code)
    if len(tokens) != len(item.events):
        norm.issues.append(f"chronology code '{code}' does not list all {len(item.events)} events")
        return
    ordered = []
    for tok in tokens:
        ordv = _ordinal_of(tok)
        entry = events_by_ord.get(ordv) if ordv is not None else None
        if entry is None:
            norm.issues.append(f"chronology code '{code}' has an unresolvable token '{tok}'")
            return
        ordered.append(entry.text)
    norm.ordered_events = ordered


# Statement option phrasing -> which statement numbers are declared true.
_ONLY_RE = re.compile(r"\bonly\s+(\d+)\b|\b(\d+)\s+only\b", re.IGNORECASE)
_BOTH_AND_RE = re.compile(r"\bboth\s+(\d+)\s+and\s+(\d+)\b", re.IGNORECASE)
_NEITHER_RE = re.compile(r"\bneither\s+(\d+)\s+nor\s+(\d+)\b", re.IGNORECASE)
_LIST_AND_ONLY_RE = re.compile(r"^\s*((?:\d+\s*,\s*)*\d+\s*(?:and|&)\s*\d+)\s+only\s*$", re.IGNORECASE)
_ALL_RE = re.compile(r"\ball\s+of\s+the\s+above\b", re.IGNORECASE)
_NONE_RE = re.compile(r"\bnone\s+of\s+the\s+above\b", re.IGNORECASE)
# Statement numbers printed as roman numerals, e.g. "(ii) and (iii)" or "I and II only".
_ROMAN_TOKEN_RE = re.compile(r"\(?\b(viii|vii|vi|iv|ix|v|x|iii|ii|i)\b\)?", re.IGNORECASE)
_CODE_WORDS_RE = re.compile(r"\b(and|only|both|neither|nor|all|of|the|above)\b", re.IGNORECASE)
_TRUE_WORDS = {"true", "correct"}
_BOTH_STATEMENTS_RE = re.compile(
    r"\bboth\s+statements?\s+(\d+)\s+and\s+(?:statement\s+)?(\d+)\s+are\s+(true|correct|false|incorrect)", re.IGNORECASE)
_EACH_STATEMENT_RE = re.compile(r"\bstatement\s+(\d+)\s+is\s+(true|correct|false|incorrect)", re.IGNORECASE)


def _parse_statement_option(text: str, n_statements: int) -> set[int] | None:
    """Return the set of 1-based statement numbers declared TRUE by this option's wording.

    None means the wording could not be confidently parsed.
    """
    t = _ROMAN_TOKEN_RE.sub(lambda m: str(ROMAN_MAP[m.group(1).lower()]), text.strip())
    # "Statement I is true, but Statement II is false" / "Both Statement I and Statement II are true".
    both = _BOTH_STATEMENTS_RE.search(t)
    if both:
        return {int(both.group(1)), int(both.group(2))} if both.group(3).lower() in _TRUE_WORDS else set()
    each = _EACH_STATEMENT_RE.findall(t)
    if each:
        return {int(n) for n, verdict in each if verdict.lower() in _TRUE_WORDS}
    if _ALL_RE.search(t):
        return set(range(1, n_statements + 1))
    if _NEITHER_RE.search(t):
        return set()
    m = _NEITHER_RE.search(t)
    m = _BOTH_AND_RE.search(t)
    if m:
        return {int(m.group(1)), int(m.group(2))}
    m = _LIST_AND_ONLY_RE.search(t)
    if m:
        nums = re.findall(r"\d+", m.group(1))
        return {int(n) for n in nums}
    m = _ONLY_RE.search(t)
    if m:
        num = m.group(1) or m.group(2)
        return {int(num)}
    # "1, 2 and 3" without "only" (used alongside an explicit statement list, e.g. all three).
    nums = re.findall(r"\d+", t)
    if nums and len(set(nums)) == len(nums) and re.search(r"\band\b", t, re.IGNORECASE):
        return {int(n) for n in nums}
    return None


def _normalize_statement(item: SourceItem, norm: Normalized) -> None:
    if not item.statements:
        norm.issues.append("statement item has no listed statements")
        return
    opt = _find_option(item, item.resolved_answer or "")
    if opt is None:
        norm.issues.append(f"resolved answer '{item.resolved_answer}' does not match any printed option")
        return
    true_nums = _parse_statement_option(opt.text, len(item.statements))
    if true_nums is None:
        norm.issues.append(f"could not decode statement option wording '{opt.text}'")
        return
    for i, stmt in enumerate(item.statements, start=1):
        norm.statements.append(StatementTruth(text=stmt.text, truth="true" if i in true_nums else "false"))


# Assertion/Reason standard option wordings.
_AR_BOTH_TRUE_EXPLAINS_RE = re.compile(r"both.*true.*r.*(correct|explain)", re.IGNORECASE)
_AR_BOTH_TRUE_NOT_EXPLAIN_RE = re.compile(r"both.*true.*r.*not.*(correct|explain)", re.IGNORECASE)
_AR_A_TRUE_R_FALSE_RE = re.compile(r"a.*true.*r.*false", re.IGNORECASE)
_AR_A_FALSE_R_TRUE_RE = re.compile(r"a.*false.*r.*true", re.IGNORECASE)


def _normalize_assertion_reason(item: SourceItem, norm: Normalized) -> None:
    if len(item.statements) < 2:
        norm.issues.append("assertion/reason item needs an Assertion and a Reason statement")
        return
    opt = _find_option(item, item.resolved_answer or "")
    if opt is None:
        norm.issues.append(f"resolved answer '{item.resolved_answer}' does not match any printed option")
        return
    text = opt.text.strip()
    assertion, reason = item.statements[0], item.statements[1]

    if _AR_BOTH_TRUE_NOT_EXPLAIN_RE.search(text):
        norm.statements = [StatementTruth(text=assertion.text, truth="true"), StatementTruth(text=reason.text, truth="true")]
        norm.reason_explains_assertion = False
    elif _AR_BOTH_TRUE_EXPLAINS_RE.search(text):
        norm.statements = [StatementTruth(text=assertion.text, truth="true"), StatementTruth(text=reason.text, truth="true")]
        norm.reason_explains_assertion = True
    elif _AR_A_TRUE_R_FALSE_RE.search(text):
        norm.statements = [StatementTruth(text=assertion.text, truth="true"), StatementTruth(text=reason.text, truth="false")]
        norm.reason_explains_assertion = None
    elif _AR_A_FALSE_R_TRUE_RE.search(text):
        norm.statements = [StatementTruth(text=assertion.text, truth="false"), StatementTruth(text=reason.text, truth="true")]
        norm.reason_explains_assertion = None
    else:
        norm.issues.append(f"could not decode assertion/reason option wording '{text}'")


def _normalize_mcq(item: SourceItem, norm: Normalized) -> None:
    opt = _find_option(item, item.resolved_answer or "")
    if opt is None:
        if item.options:
            norm.issues.append(f"resolved answer '{item.resolved_answer}' does not match any printed option")
            return
        # No options at all: the resolved answer text itself may already be the direct answer.
        if item.resolved_answer:
            norm.correct_answer_text = item.resolved_answer.strip()
        else:
            norm.issues.append("mcq item has no options and no resolved answer")
        return
    norm.correct_answer_text = opt.text.strip()
    # An answer like "1 and 3 only" or "(i) and (iii)" only means something together with the
    # numbered list it refers to. Without that list there is no fact to state.
    residue = re.sub(r"[\d\W_]", "", _ROMAN_TOKEN_RE.sub("", _CODE_WORDS_RE.sub("", opt.text)))
    if not item.statements and not residue and _parse_statement_option(opt.text, 0) is not None:
        norm.issues.append(f"answer '{opt.text}' refers to numbered items that were not captured")


def normalize(item: SourceItem) -> Normalized:
    norm = Normalized(type=item.type, question=item.stem)

    if item.type == "info_box":
        # No decoding here: generation extracts facts straight from raw_content/explanation.
        return norm

    if item.type == "unsupported":
        return norm

    if item.answer_source in ("none", "conflict"):
        norm.issues.append(f"answer_source is '{item.answer_source}'")
        return norm

    if not item.resolved_answer:
        norm.issues.append("no resolved_answer set")
        return norm

    if item.type == "matching":
        _normalize_matching(item, norm)
    elif item.type == "chronology":
        _normalize_chronology(item, norm)
    elif item.type == "statement":
        _normalize_statement(item, norm)
    elif item.type == "assertion_reason":
        _normalize_assertion_reason(item, norm)
    elif item.type == "mcq":
        _normalize_mcq(item, norm)
    else:
        norm.issues.append(f"unknown item type '{item.type}'")

    return norm
