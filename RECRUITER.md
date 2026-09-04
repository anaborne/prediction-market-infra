# What this repository is

This repository holds trading software for prediction markets, exchanges where people buy and sell
contracts on whether an event happens. It runs as two programs on one machine. One watches market
data and decides when to trade, the other sends the order to the exchange, and they are kept
separate so the deciding work never holds up the program that has to get an order out quickly. A
second piece, the matcher, takes the market lists from two exchanges, roughly a billion possible
pairings, and finds the pairs about the same event. It accepts a pair only if the two markets would
pay out the same way. The benchmark published here times two parts of that path on whoever's
computer runs it, signing an order and passing one message between the two programs. It does not
place an order with a real exchange, so it is not an end to end trading speed. The code is an
extraction from a larger private system, published so its claims can be checked by someone without
that system.
