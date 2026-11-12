"""``python -m ownvitals.withings.probe_assumptions_cli [--json]`` (#450).

Reads the probe's ``--json`` report on stdin and prints one verdict per documented assumption
(``probe_assumptions.py``). It makes no call and opens no file or database: the probe is the only
part that reaches Withings. The owner's local runbook (``docs/dev/local-real-account.md``) pipes
the probe's ``--json`` output into it inside the worker container.

Exit code 0: no ``fail``; 1: at least one ``fail``; 2: the input is not a probe report (the
message is fixed and never quotes the input).
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
    text = src.read(MAX_INPUT + 1)
    try:
        if len(text) > MAX_INPUT:
            raise NotAProbeReport
        try:
            report = json.loads(text)
        except ValueError:
            raise NotAProbeReport from None
        checks = evaluate(report)
    except NotAProbeReport as refused:
        print(f"error: {refused}", file=err)
        return 2
    print(json.dumps(as_dict(checks), indent=2) if args.json else render(checks), file=out)
    return 1 if any(c.verdict == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
