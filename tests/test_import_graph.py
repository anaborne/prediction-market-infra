"""The structural guarantees of `docs/GUIDE.md` §7.3, asserted rather than described.

Three of the guarantees the documentation makes are claims about what a package *cannot* reach:
`crossvenue` holds no signer and cannot place an order, `transport` does not write telemetry, and
`runtime` is a leaf. A sentence saying so decays silently the first time someone adds a convenient
import (§6: "a structural guarantee written only in prose decays silently"), so each is pinned
here as a test that reads the import graph and fails the moment the graph grows the wrong edge.

Each check runs in a fresh interpreter. The pytest process itself has long since imported every
package under test, so `sys.modules` in-process would show the union of everything the suite
touched rather than what one package pulls in on its own.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys
from types import ModuleType

import kalshi_bot.crossvenue
import kalshi_bot.runtime
import kalshi_bot.transport


def _submodules(package: ModuleType) -> list[str]:
    return [f"{package.__name__}.{info.name}" for info in pkgutil.iter_modules(package.__path__)]


def _loaded_after_importing(modules: list[str]) -> set[str]:
    """Import `modules` in a clean interpreter and return every `kalshi_bot.*` module loaded."""
    script = (
        "import sys, importlib\n"
        f"for m in {modules!r}:\n"
        "    importlib.import_module(m)\n"
        "print('\\n'.join(sorted(n for n in sys.modules if n.startswith('kalshi_bot'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    return {line for line in result.stdout.splitlines() if line}


def test_crossvenue_imports_neither_execution_nor_auth_nor_transport() -> None:
    """`crossvenue` cannot place an order because the types that can are not in scope."""
    loaded = _loaded_after_importing(_submodules(kalshi_bot.crossvenue))
    forbidden = {
        n
        for n in loaded
        if n.startswith(("kalshi_bot.execution", "kalshi_bot.auth", "kalshi_bot.transport"))
    }
    assert not forbidden, f"crossvenue reached write-capable packages: {sorted(forbidden)}"


def test_transport_does_not_import_telemetry() -> None:
    """Timings only `transport` can see come back through `RequestTimings`; `execution` writes."""
    loaded = _loaded_after_importing(_submodules(kalshi_bot.transport))
    telemetry = {n for n in loaded if n.startswith("kalshi_bot.telemetry")}
    assert not telemetry, f"transport imported telemetry: {sorted(telemetry)}"


def test_runtime_is_a_leaf() -> None:
    """`runtime` imports nothing else in the codebase; that is what lets everything depend on it."""
    loaded = _loaded_after_importing(_submodules(kalshi_bot.runtime))
    outside = {n for n in loaded if n != "kalshi_bot" and not n.startswith("kalshi_bot.runtime")}
    assert not outside, f"runtime imported other kalshi_bot packages: {sorted(outside)}"
