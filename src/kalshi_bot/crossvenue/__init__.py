"""Cross-venue market matching and arbitrage scanning (Kalshi <-> Polymarket).

This package is read-only by construction. Nothing in it holds a signer, a private key, or a
write-capable client, and no module here imports `kalshi_bot.execution`. Its output is a research
dataset of matched market pairs and the executable edge implied by their order books, never
orders.

Why that matters is `venues.py`: the venue a US-resident operator may legally execute on is not
the venue whose public order book is easiest to read. Encoding that difference in the type system,
rather than in a comment, is the point of this package's structure.
"""
