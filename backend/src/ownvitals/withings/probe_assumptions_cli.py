"""``python -m ownvitals.withings.probe_assumptions_cli [--json]`` (#450).

Reads the probe's ``--json`` report on stdin and prints one verdict per documented assumption
(``probe_assumptions.py``). It makes no call and opens no file or database: the probe is the only
part that reaches Withings. The owner's local runbook (``docs/dev/local-real-account.md``) pipes
the probe's ``--json`` output into it inside the worker container.

Exit code 0: no ``fail``; 1: at least one ``fail``; 2: the input is not a probe report, or
anything else went wrong while reading or checking it. The message is fixed and never quotes the
input: no exception text or traceback is printed, since either could hold a value from it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Final, TextIO

from ownvitals.withings.probe_assumptions import (
    FAIL,
    NotAProbeReport,
    as_dict,
    evaluate,
    render,
)

MAX_INPUT: Final = 1_000_000  # characters; the probe's report is a few kilobytes


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_assumptions_cli", description="verdicts per documented Withings assumption"
    )
    parser.add_argument("--json", action="store_true", help="print the verdicts as JSON")
    args = parser.parse_args(argv)
    src, out, err = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    try:
        text = src.read(MAX_INPUT + 1)
        if len(text) > MAX_INPUT:
            raise NotAProbeReport
        checks = evaluate(json.loads(text))
        result = json.dumps(as_dict(checks), indent=2) if args.json else render(checks)
    except Exception:  # any failure gets the one fixed message, never its text
        print(f"error: {NotAProbeReport()}", file=err)
        return 2
    print(result, file=out)
    return 1 if any(c.verdict == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
