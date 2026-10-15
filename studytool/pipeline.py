"""Run one job's stages. OWNER: agent A.

Stages, each step committed before the next (job_steps row per stage+item_key):
  extract  item_key 'page:<n>'   -> pages row (skip if cache_key already matches)
  segment  item_key 'page:<n>'   -> segment.segment_page; upsert source_items + evidence
  link     item_key 'all'        -> link.link_answers over all items + key entries of the deck
  generate item_key <item_key>   -> generate.generate_cards; insert cards (card_key unique),
                                    card_evidence, card_revisions(origin='generated'), model_calls
  dedupe   item_key 'all'        -> same fingerprint as an earlier card -> duplicate_of set,
                                    status needs_review with reason; item disposition 'duplicate'
                                    when all its cards are duplicates
Only content pages produce cards; key pages are extracted/segmented for evidence only.
AI calls run outside transactions, with at most config.MAX_AI_CONCURRENCY at a time.
Cancellation is checked before each new AI call.
"""
