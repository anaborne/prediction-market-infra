"""Process-level runtime primitives: clocks, instance locking, logging, and the kill switch.

Deliberately a leaf package. Nothing here imports `config`, `telemetry`, `transport`, or any
trading module, so both hot-path processes and the standalone scripts can use it without dragging
in the trading stack, and so a bug here cannot be caused by one.
"""
