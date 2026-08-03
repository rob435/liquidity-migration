"""Quote lab: replay a recorded market tape and score virtual resting quotes.

``tape`` reads recorded records back, ``book`` mirrors the level-2 book
(displayed orders per price), ``shadow`` rests virtual post-only orders
against the replay, ``summary`` aggregates the outcomes.
"""
