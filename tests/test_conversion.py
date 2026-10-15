"""Tests for normalize.py, generate.py (blocking logic only), and validate.py. No network calls."""
import pytest

from studytool import ai, normalize, validate
from studytool.schemas import CardDraft, Option, SourceItem


def _hastinapur_item(code: str, scrambled=True) -> SourceItem:
    """Matching item: Site <-> Excavator. The screenshot's Hastinapur example."""
    list_i = [
        Option(label="A", text="Hastinapur"),
        Option(label="B", text="Ahichhatra"),
        Option(label="C", text="Shringaverapura"),
        Option(label="D", text="Kaushambi"),
    ]
    # List-II rows deliberately scrambled relative to the correct pairing.
    if scrambled:
        list_ii = [
            Option(label="1", text="B.K. Thapar (1951-52)"),
            Option(label="2", text="G.R. Sharma (1957-59)"),
            Option(label="3", text="B.B. Lal (1950-52)"),
            Option(label="4", text="B.B. Lal and K.N. Dikshit (1976-77)"),
        ]
    else:
        list_ii = [
            Option(label="1", text="B.B. Lal (1950-52)"),
            Option(label="2", text="B.K. Thapar (1951-52)"),
            Option(label="3", text="G.R. Sharma (1957-59)"),
            Option(label="4", text="B.B. Lal and K.N. Dikshit (1976-77)"),
        ]
    options = [Option(label="a", text=code)]
    return SourceItem(
        item_key="main|1|p1",
        type="matching",
        page_refs=[1],
        raw_content="Match List I with List II.",
        stem="Match List I with List II and select the correct answer using the code given below.",
        list_i_title="Site",
        list_ii_title="Excavator",
        list_i=list_i,
        list_ii=list_ii,
        options=options,
        resolved_answer="a",
        answer_source="answer_key",
        explanation="Hastinapur-B.B. Lal (1950-52); Ahichhatra-B.K. Thapar (1951-52); "
                    "Shringaverapura-G.R. Sharma (1957-59); Kaushambi-B.B. Lal and K.N. Dikshit (1976-77).",
    )


def _expect_pairs(rels):
    pairs = {(r.left, r.right) for r in rels}
    assert pairs == {
        ("Hastinapur", "B.B. Lal (1950-52)"),
        ("Ahichhatra", "B.K. Thapar (1951-52)"),
        ("Shringaverapura", "G.R. Sharma (1957-59)"),
        ("Kaushambi", "B.B. Lal and K.N. Dikshit (1976-77)"),
    }


def test_matching_pair_code_decodes_correctly():
    item = _hastinapur_item("A-3, B-1, C-2, D-4")
    norm = normalize.normalize(item)
    assert not norm.issues
    _expect_pairs(norm.relationships)
    assert norm.relationships[0].left_meaning == "Site"
    assert norm.relationships[0].right_meaning == "Excavator"


def test_matching_roman_numeral_code_decodes():
    # List-II here uses roman-numeral labels i..iv instead of 1..4, same scrambled order.
    item = _hastinapur_item("A-iii B-i C-ii D-iv")
    # relabel list_ii with roman numerals matching the same scrambled rows
    for opt, roman in zip(item.list_ii, ["i", "ii", "iii", "iv"]):
        opt.label = roman
    norm = normalize.normalize(item)
    assert not norm.issues
    _expect_pairs(norm.relationships)


def test_matching_grid_form_decodes():
    item = _hastinapur_item("A B C D\n3 1 2 4")
    norm = normalize.normalize(item)
    assert not norm.issues
    _expect_pairs(norm.relationships)


def test_matching_bare_number_sequence_decodes():
    # "3 1 2 4" with no letters: position order left (A,B,C,D) implied by list order.
    item = _hastinapur_item("3 1 2 4")
    norm = normalize.normalize(item)
    assert not norm.issues
    _expect_pairs(norm.relationships)


def test_chronology_letter_code_expands_to_names():
    events = [
        Option(label="A", text="Battle of Plassey"),
        Option(label="B", text="Battle of Buxar"),
        Option(label="C", text="Regulating Act"),
    ]
    item = SourceItem(
        item_key="main|2|p1",
        type="chronology",
        page_refs=[1],
        raw_content="Arrange in chronological order.",
        stem="Arrange the following in chronological order.",
        events=events,
        options=[Option(label="a", text="B A C")],
        resolved_answer="a",
        answer_source="answer_key",
    )
    norm = normalize.normalize(item)
    assert not norm.issues
    assert norm.ordered_events == ["Battle of Buxar", "Battle of Plassey", "Regulating Act"]


def test_chronology_numeric_dash_code_expands():
    events = [
        Option(label="1", text="Event One"),
        Option(label="2", text="Event Two"),
        Option(label="3", text="Event Three"),
    ]
    item = SourceItem(
        item_key="main|3|p1",
        type="chronology",
        page_refs=[1],
        raw_content="Arrange in order.",
        events=events,
        options=[Option(label="a", text="2-1-3")],
        resolved_answer="a",
        answer_source="answer_key",
    )
    norm = normalize.normalize(item)
    assert norm.ordered_events == ["Event Two", "Event One", "Event Three"]


def test_statements_1_and_3_only_gives_truths():
    statements = [
        Option(label="1", text="Statement one"),
        Option(label="2", text="Statement two"),
        Option(label="3", text="Statement three"),
    ]
    item = SourceItem(
        item_key="main|4|p1",
        type="statement",
        page_refs=[1],
        raw_content="Which of the statements is/are correct?",
        statements=statements,
        options=[Option(label="a", text="1 and 3 only")],
        resolved_answer="a",
        answer_source="answer_key",
    )
    norm = normalize.normalize(item)
    assert not norm.issues
    truth_by_text = {s.text: s.truth for s in norm.statements}
    assert truth_by_text["Statement one"] == "true"
    assert truth_by_text["Statement two"] == "false"
    assert truth_by_text["Statement three"] == "true"


def test_assertion_reason_both_true_explains():
    statements = [
        Option(label="A", text="Assertion text"),
        Option(label="R", text="Reason text"),
    ]
    item = SourceItem(
        item_key="main|5|p1",
        type="assertion_reason",
        page_refs=[1],
        raw_content="Assertion/Reason question.",
        statements=statements,
        options=[
            Option(label="a", text="Both A and R are true and R is the correct explanation of A"),
            Option(label="b", text="Both A and R are true but R is not the correct explanation of A"),
        ],
        resolved_answer="a",
        answer_source="answer_key",
    )
    norm = normalize.normalize(item)
    assert not norm.issues
    assert norm.reason_explains_assertion is True
    assert all(s.truth == "true" for s in norm.statements)


def test_missing_answer_blocks_with_no_ai_call(monkeypatch):
    from studytool import generate

    def _boom(*a, **k):
        raise AssertionError("ai.call_json must not be called when the answer is missing")

    monkeypatch.setattr(ai, "call_json", _boom)
    item = SourceItem(
        item_key="main|6|p1",
        type="mcq",
        page_refs=[1],
        raw_content="Some MCQ with no resolved answer.",
        options=[Option(label="a", text="X"), Option(label="b", text="Y")],
        resolved_answer=None,
        answer_source="none",
    )
    result = generate.generate_cards(item)
    assert result.disposition == "blocked"
    assert result.issue_reason
    assert result.cards == []


def test_incorrectly_matched_pair_goes_to_rejected():
    item = _hastinapur_item("", scrambled=False)
    item.stem = "Which one of the following pairs is not correctly matched?"
    item.options = [
        Option(label="a", text="Hastinapur - B.K. Thapar (1951-52)"),
    ]
    item.resolved_answer = "a"
    norm = normalize.normalize(item)
    assert not norm.issues
    assert len(norm.rejected_relationships) == 1
    assert norm.rejected_relationships[0].left == "Hastinapur"
    assert norm.rejected_relationships[0].right == "B.K. Thapar (1951-52)"


def test_check_card_flags_raw_chronology_code():
    item = SourceItem(
        item_key="main|7|p1", type="chronology", page_refs=[1], raw_content="x",
        events=[Option(label="A", text="e1"), Option(label="B", text="e2"), Option(label="C", text="e3")],
    )
    card = CardDraft(front="Arrange these events.", back="BAC")
    flags = validate.check_card(card, item)
    assert "answer_code_leak" in flags


def test_check_card_flags_option_letter_code():
    item = SourceItem(item_key="main|8|p1", type="mcq", page_refs=[1], raw_content="x", options=[Option(label="a", text="y")])
    card = CardDraft(front="What is the answer?", back="(c)")
    flags = validate.check_card(card, item)
    assert "answer_code_leak" in flags


def test_check_card_flags_source_dependent_wording():
    item = SourceItem(item_key="main|9|p1", type="matching", page_refs=[1], raw_content="x")
    card = CardDraft(front="In List-I, what pairs with X in List II?", back="Y")
    flags = validate.check_card(card, item)
    assert "source_dependent_wording" in flags


def test_fingerprint_ignores_case_spacing_punctuation():
    f1 = validate.fingerprint("Who excavated Hastinapur?", "B.B. Lal.")
    f2 = validate.fingerprint("who   excavated hastinapur", "bb lal")
    assert f1 == f2
