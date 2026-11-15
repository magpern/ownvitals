"""The owner's diagnostic dump of the evaluation's failed cases (``--failed-file``; #527).

For every case that failed grading, or failed to run, one JSON line: the label, the category,
the synthetic prompt the model was given, its answer, the tool calls it made, the tool results
of the turn and the grader's reasons (``eval_model.Reason``: a closed code and the offending
token). It exists so the owner can tell a model failure from a prompt problem or a grader false
alarm. The data is synthetic (the database is checked synthetic before every call), yet the file
is treated like the review file: owner-only (0600), its path only from the flag, never printed
or logged. A passing case is never written. The file is created (or emptied) before the first
call and rewritten at the end; a run whose cases all pass leaves it empty.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ownvitals.ai.eval_locks import EvalRefused
from ownvitals.ai.eval_model import Case, Reason
from ownvitals.ai.requests import ToolUse


@dataclass(frozen=True, slots=True)
class Failure:
    """One failed case: what the model saw and said, and why the grader (or the run) failed it."""

    label: str
    category: str
    prompt: str
    answer: str | None
    stopped: str  # how the turn ended, or ``not_run`` / ``model_failed``
    uses: tuple[ToolUse, ...]
    results: tuple[Mapping[str, Any], ...]
    reasons: tuple[Reason, ...]


def did_not_run(case: Case, prompt: str, code: str) -> Failure:
    """A case with no answer: its request failed (``model_failed``) or was never sent."""
    return Failure(case.label, case.category, prompt, None, code, (), (), (Reason(code),))


def _line(f: Failure) -> str:
    return json.dumps(
        {
            "label": f.label,
            "category": f.category,
            "prompt": f.prompt,
            "answer": f.answer,
            "stopped": f.stopped,
            "tool_calls": [{"name": u.name, "arguments": dict(u.arguments)} for u in f.uses],
            "tool_results": list(f.results),
            "reasons": [{"code": r.code, "detail": r.detail} for r in f.reasons],
        },
        default=str,
    )


def _open(path: Path) -> int:
    """Owner-only, never through a symbolic link; an existing file is emptied."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def prepare_failed_file(path: Path) -> None:
    """Before any call: refuse a path that cannot be written (a link, a missing directory), so a
    paid run is never lost to it."""
    try:
        os.close(_open(path))
    except OSError:
        raise EvalRefused("failed_file_refused") from None


def write_failed(path: Path, failures: list[Failure]) -> None:
    fd = _open(path)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.writelines(_line(f) + "\n" for f in failures)
