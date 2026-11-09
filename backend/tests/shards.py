"""The CI split of the backend tests into shards (`.github/workflows/ci.yml`, `backend-tests`).

A test file belongs to shard ``int(sha256(path), 16) % total + 1``: stable, needs no list to keep
up to date, and spreads the files evenly (280 files over four shards: 82, 68, 64 and 66). No
dependency beyond the standard library.

    python tests/shards.py list K N    # the files of shard K (1-based) of N, one per line
    python tests/shards.py check N     # every file pytest collects is in exactly one of N shards

``check`` runs pytest's own collection (``--collect-only``) and compares the files it found with
the shards: a file pytest would collect but no shard runs, or one in two shards, fails it. It
also fails when a shard is empty, because ``pytest`` with no file argument would run everything.
Run from ``backend/``.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

TESTS = Path("tests")


def test_files() -> list[str]:
    """Every ``test_*.py`` under ``tests/`` (pytest's default ``python_files``), as posix paths."""
    return sorted(p.as_posix() for p in TESTS.rglob("test_*.py"))


def shard_of(path: str, total: int) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest(), 16) % total + 1


def shard(index: int, total: int, files: list[str]) -> list[str]:
    return [f for f in files if shard_of(f, total) == index]


def collected() -> set[str]:
    """The files pytest itself collects tests from (no database needed: nothing runs)."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        sys.stderr.write(out.stdout[-4000:] + out.stderr[-4000:])
        raise SystemExit("pytest --collect-only failed")
    return {line.split("::", 1)[0] for line in out.stdout.splitlines() if "::" in line}


def check(total: int) -> list[str]:
    files = test_files()
    shards = [shard(i, total, files) for i in range(1, total + 1)]
    problems = [f"shard {i} of {total} is empty" for i, s in enumerate(shards, 1) if not s]
    runs: dict[str, int] = {}
    for s in shards:
        for f in s:
            runs[f] = runs.get(f, 0) + 1
    problems += [f"{f} runs in {n} shards" for f, n in sorted(runs.items()) if n != 1]
    found = collected()
    problems += [f"{f}: pytest collects it but no shard runs it" for f in sorted(found - set(runs))]
    # A test_*.py pytest finds no test in (all skipped at import, say) is still run by its shard.
    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "list":
        index, total = int(argv[1]), int(argv[2])
        if not 1 <= index <= total:
            raise SystemExit(f"shard {index} is not between 1 and {total}")
        files = shard(index, total, test_files())
        if not files:
            raise SystemExit(f"shard {index} of {total} is empty")
        print("\n".join(files))
        return 0
    if len(argv) == 2 and argv[0] == "check":
        total = int(argv[1])
        if total < 1:
            raise SystemExit("at least one shard")
        problems = check(total)
        for p in problems:
            print(f"::error::{p}")
        if not problems:
            print(f"{len(test_files())} test files, each in exactly one of {total} shards")
        return 1 if problems else 0
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
