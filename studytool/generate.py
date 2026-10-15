"""Write cards from normalized facts. OWNER: agent C."""
from studytool.schemas import GenerationResult, SourceItem


def generate_cards(item: SourceItem) -> GenerationResult:
    """normalize() the item, then ask the model to phrase standalone cards.

    - Items with from_key_pages_only, type 'unsupported', or blocking issues
      (no answer, conflict, undecodable code) get no AI call: disposition
      'skipped' or 'blocked' with issue_reason.
    - One card per verified relationship / fact / ordering, per PRD section 7.
    - Every card runs validate.check_card(); any flag sets review_reason.
    """
    raise NotImplementedError
