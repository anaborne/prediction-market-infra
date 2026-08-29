# Engineering rules

The standards this code is held to. Most of them were written after something went wrong, and
[`docs/GUIDE.md`](docs/GUIDE.md) §6 names the instance behind each.

## Never guess

Every factual claim comes from a direct reference, a real response, or a measurement. Memory,
plausibility, and what a similar API does are not sources. The rule applies to wire shapes, measured
numbers, file paths, and claims about what the code does.

- A 404 on a URL you constructed is evidence about the URL, and evidence about nothing else. This
  cost the parent project twice.
- If something cannot be verified, write "not verified" and name what would verify it. Do not
  supply the most likely answer.

## The one habit this codebase depends on most

Before adding or changing any wire-field access, print a real response and read the keys. A green
test suite cannot catch a wire-shape error, because the fakes encode whatever the author believed.
Four separate multi-phase bugs in the parent project came from skipping this. Absence of a
capability is a claim about the API, and only the API reference or a real response can support it.

## Commands

Dependency management is via [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                        # install dependencies
uv run pytest                  # full test suite, no network access required
uv run ruff check . && uv run ruff format --check .
uv run mypy                    # strict mode
uv run pre-commit install
```

## Non-negotiable coding rules

1. Never put pandas, dataframes, or an ORM in the execution path. State is pre-computed or
   pre-loaded at startup. Nothing is queried at fire time.
2. Always write complete, runnable files. No `# rest of code` placeholders.
3. Every component ships with a standalone pytest or benchmark script.
4. Telemetry writes (SQLite) are fire-and-forget. Nothing awaits one before an order is submitted.
5. No test may touch a live API. Network-facing code is tested against local `aiohttp`/`websockets`
   servers on ephemeral ports, or against fakes.
6. One number, one home. Every measured figure lives in `docs/GUIDE.md` and is cited from there.
   Copying a number into a second file is how documentation drifts into contradiction, and it is
   the reason a test count in a README went stale by nine while every gate stayed green.

## Architecture

Module boundaries and the structural guarantees are in [`docs/GUIDE.md`](docs/GUIDE.md) §7. The
boundaries that are load-bearing and easy to erode:

- `transport` must not import `telemetry`.
- `crossvenue` must hold no signer, no key material, and must not import `execution`.
- `runtime` is a leaf. It imports nothing else in the codebase.
- `OrderDispatcher` cannot be constructed without a permission guard.

Each is enforced by a mechanism, the first three by `tests/test_import_graph.py`, which reads each
package's import graph in a fresh interpreter, and the fourth by a required constructor argument.
Keep it that way. A guarantee written only in prose decays silently, and §6 records the occasion
when one of these was documented for an unknown period while not existing in the code at all.

## Finishing a step: resolve every flag you raised

A step is not complete while a flag it raised is still open. A flag is anything the work surfaced
that the work depends on: a caveat, a stale number, a contradiction, a degree of freedom taken, a
defect found in passing, a premise that changed underneath you. At the end of every step:

1. Enumerate the flags, including inherited ones the work touched.
2. Resolve each as exactly one of Fixed (say what and where), Recorded (a real limitation written
   down, with what would resolve it), or Withdrawn (with the reason). Silently dropping a flag is
   none of these.
3. Report the flags and their dispositions when declaring the step done. "This step raised no
   flags" is a claim, and it is usually wrong.

Numbers other components consume are flags by default. If a step changes a measured figure, grep
for every place it is baked in. The parent project shipped two components against a number that had
moved underneath them.

## Documentation discipline

| File | Holds |
|---|---|
| `README.md` | What this is, what it measures, how to run it. |
| `ENGINEERING.md` | This file. The rules, and why each exists. Carries almost no measured numbers. |
| `docs/GUIDE.md` | Architecture (§7), method (§5), failure modes (§6), results (§4). All measured numbers live here. |
| `docs/configuration.md` | Every environment variable. |
| `docs/incidents/` | Postmortems. |

- A reversal is recorded. Wrong conclusions stay in the record with the reason they were wrong,
  because the failure modes repeat.
- `docs/` is public and must read well for an outsider.

## Runtime

- Python 3.12, dependency-managed via `uv`.
- Event loop: `uvloop` on the hot path, adopted after benchmarking it against stock `asyncio`.
- Serialization: `orjson` in `transport`, `execution`, `ipc`, and `crossvenue`. Stdlib `json`
  elsewhere is fine.
