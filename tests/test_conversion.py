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




# ---------- ai.py: DeepSeek error handling and JSON repair ----------

def _fake_body(content: str) -> dict:
    return {"model": "deepseek-flash", "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 0}}


def test_call_json_repairs_one_invalid_reply(monkeypatch):
    replies = iter([_fake_body('{"front": "Q"}'),  # missing "back"
                    _fake_body('{"front": "Who excavated Hastinapur?", "back": "B.B. Lal."}')])
    sent = []
    monkeypatch.setattr(ai, "_post", lambda messages: (sent.append(list(messages)), next(replies))[1])
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    card, usage = ai.call_json("system", "prompt", CardDraft, "v1")
    assert card.back == "B.B. Lal."
    assert "does not match the schema" in sent[1][-1]["content"]  # error was shown to the model
    assert usage.input_tokens == 200 and usage.output_tokens == 40  # both calls counted


def test_call_json_stops_at_once_on_permanent_error(monkeypatch):
    calls = []

    def _no_balance(messages):
        calls.append(1)
        raise ai.AIPermanentError("DeepSeek API error 402: no balance")

    monkeypatch.setattr(ai, "_post", _no_balance)
    with pytest.raises(ai.AIPermanentError, match="402"):
        ai.call_json("system", "prompt", CardDraft, "v1")
    assert len(calls) == 1


# ---------- found on a real exam book (Ghatna Chakra) ----------

def test_roman_numeral_statement_codes_decode():
    from studytool.normalize import _parse_statement_option
    assert _parse_statement_option("(ii) and (iii)", 3) == {2, 3}
    assert _parse_statement_option("Only (i)", 3) == {1}


def test_mcq_whose_answer_is_only_item_numbers_is_blocked():
    item = SourceItem(
        item_key="main|9|p31", type="mcq", page_refs=[31], raw_content="x",
        options=[Option(label="c", text="(i) and (iii)")], resolved_answer="c", answer_source="inline",
    )
    assert normalize.normalize(item).issues


def test_star_answer_is_not_an_answer():
    from studytool.link import link_answers
    item = SourceItem(item_key="main|2|p10", type="mcq", page_refs=[10], raw_content="x",
                      question_number="2", options=[Option(label="a", text="y")], inline_answer="*")
    linked = link_answers([item], [])[0]
    assert linked.resolved_answer is None and "'*'" in linked.issue_reason


def test_year_answer_is_not_a_code_leak():
    item = SourceItem(item_key="main|3|p2", type="mcq", page_refs=[2], raw_content="passed in 1784")
    assert "answer_code_leak" not in validate.check_card(CardDraft(front="When?", back="1784"), item)


def test_statement_i_ii_wording_decodes():
    from studytool.normalize import _parse_statement_option
    assert _parse_statement_option("Statement I is true, but Statement II is false.", 2) == {1}
    assert _parse_statement_option("Both Statement I and Statement II are true.", 2) == {1, 2}
    assert _parse_statement_option("Both Statement I and Statement II are false.", 2) == set()
