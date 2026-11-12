"""The scores pass's pure step (ADR 0012): plain inputs in, one ``ScoreResult`` per kind out.

No database, file or clock: ``derived.scores_gather`` reads the rows and ``derived.scores_store``
writes the results. Sleep and activity values come as ``(day, value)`` points per observation
key (``normalize_stream_spec.SCORE_INPUTS`` names the keys). Assumptions, until the probe
confirms them: ``sleep_total_sleep_time`` is in seconds; ``activity_moderate`` and
``activity_intense`` are in seconds per day (ADR 0012 §2: active minutes).
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.scores import ScoreResult, Status, body_path, his_inputs
from ownvitals.analysis.scores.his import CriterionInput, evaluate_his
from ownvitals.analysis.scores.his_table import BandTable
from ownvitals.analysis.scores.readiness import Night, evaluate_readiness
from ownvitals.derived.normalize_stream_spec import SCORE_INPUTS

FINGERPRINT_TAG: Final = "scores-inputs/1"
BODY_KEYS: Final = ("weight", "muscle_mass", "fat_mass")
DAILY_KEYS: Final = (*BODY_KEYS, "fat_ratio", "vascular_age")
SLEEP_KEYS: Final = tuple(sorted({v for k, v in SCORE_INPUTS.items() if v.startswith("sleep_")}))
ACTIVITY_KEYS: Final = tuple(
    sorted({v for k, v in SCORE_INPUTS.items() if v.startswith("activity_")})
)
OBSERVATION_KEYS: Final = (*SLEEP_KEYS, *ACTIVITY_KEYS)
type Points = tuple[tuple[date, float], ...]


@dataclass(frozen=True, slots=True)
class ScoreInputs:
    day: date
    daily: Mapping[str, Sequence[DailyValue]]
    points: Mapping[str, Points]
    birth_year: int | None
    sex: str | None
    table: BandTable | None
    table_error: str | None
    table_digest: str
    experimental: bool
    day_tz: str | None = None
    # The newest confirmed height observation (Withings meastype 4): (ts ISO, metres). Read under
    # RLS by ``scores_gather``; never guessed, so None means ``no_height``.
    height: tuple[str, float] | None = None
    extra: Mapping[str, str] = field(default_factory=dict)


def kinds(inputs: ScoreInputs) -> list[str]:
    """The kinds this pass writes for ``inputs.day`` (his only behind the experimental switch)."""
    out = [f"body_path:{key}" for key in BODY_KEYS] + ["readiness"]
    return [*out, "his"] if inputs.experimental else out


def _nights(points: Mapping[str, Points]) -> list[Night]:
    keys = {
        "duration": SCORE_INPUTS["readiness_sleep_duration"],
        "hr": SCORE_INPUTS["readiness_night_hr"],
        "hrv": SCORE_INPUTS["readiness_hrv"],
    }
    by_day: dict[date, dict[str, float]] = defaultdict(dict)
    for name, key in keys.items():
        for d, v in points.get(key, ()):
            by_day[d][name] = v
    return [
        Night(d, v["duration"] / 3600 if "duration" in v else None, v.get("hr"), v.get("hrv"))
        for d, v in sorted(by_day.items())
    ]


def _his_inputs(inputs: ScoreInputs) -> dict[str, CriterionInput]:
    day, p = inputs.day, inputs.points
    age = None if inputs.birth_year is None else day.year - inputs.birth_year

    def series(key: str) -> list[tuple[date, float | None]]:
        return [(d, v) for d, v in p.get(key, ())]

    def daily(key: str) -> list[tuple[date, float | None]]:
        return [(d.day, float(d.value)) for d in inputs.daily.get(key, ())]

    moderate = dict(p.get(SCORE_INPUTS["weekly_active_minutes_moderate"], ()))
    intense = dict(p.get(SCORE_INPUTS["weekly_active_minutes_intense"], ()))
    minutes = [
        (d, (moderate.get(d, 0.0) + intense.get(d, 0.0)) / 60) for d in sorted(moderate | intense)
    ]
    unwired = CriterionInput(Status.NOT_AVAILABLE, reason="input_not_wired")
    return {
        "bmi": his_inputs.bmi(
            daily("weight"), None if inputs.height is None else inputs.height[1], day
        ),
        "fat_ratio": his_inputs.median_criterion(
            daily("fat_ratio"), day, days=30, minimum=4, unit="%"
        ),
        "night_hr": his_inputs.median_criterion(
            series(SCORE_INPUTS["night_hr"]), day, days=30, minimum=7, unit="bpm"
        ),
        "vascular_age_gap": his_inputs.vascular_age_gap(daily("vascular_age"), age, day),
        "afib_ecg_count": unwired,
        "high_bp_ratio": unwired,
        "daily_steps": his_inputs.median_criterion(
            series(SCORE_INPUTS["daily_steps"]), day, days=30, minimum=14, unit="steps"
        ),
        "weekly_active_minutes": his_inputs.weekly_active_minutes(minutes, day),
        "sleep_ahi": his_inputs.median_criterion(
            series(SCORE_INPUTS["sleep_ahi"]), day, days=30, minimum=7, unit="events/h"
        ),
    }


def compute_scores(inputs: ScoreInputs) -> dict[str, ScoreResult]:
    """Kind -> result for ``inputs.day`` (pure)."""
    out: dict[str, ScoreResult] = {}
    for key in BODY_KEYS:
        res = body_path.evaluate_body_path(key, inputs.daily.get(key, ()), day=inputs.day)
        out[f"body_path:{key}"] = ScoreResult(
            f"body_path:{key}", res.algo_version, res.status, res.value, res.components
        )
    out["readiness"] = evaluate_readiness(_nights(inputs.points), day=inputs.day)
    if inputs.experimental:
        out["his"] = evaluate_his(
            inputs.table,
            table_error=inputs.table_error,
            inputs=_his_inputs(inputs),
            day=inputs.day,
            birth_year=inputs.birth_year,
            sex=inputs.sex,
        )
    return out


def fingerprint(inputs: ScoreInputs, revisions: tuple[int, ...]) -> bytes:
    """SHA-256 over the pass version, the stream revisions and every input (PLAN §5)."""
    doc = {
        "v": FINGERPRINT_TAG,
        "rev": list(revisions),
        "day": inputs.day.isoformat(),
        "tz": inputs.day_tz,
        "profile": [inputs.birth_year, inputs.sex],
        "height": None if inputs.height is None else [inputs.height[0], repr(inputs.height[1])],
        "experimental": inputs.experimental,
        "table": [inputs.table_digest, inputs.table_error],
        "daily": {
            k: [[str(d.day), str(d.value), d.method.value, d.n, str(d.mad)] for d in v]
            for k, v in sorted(inputs.daily.items())
        },
        "points": {k: [[str(d), repr(v)] for d, v in v] for k, v in sorted(inputs.points.items())},
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).digest()


__all__ = ["ScoreInputs", "compute_scores", "fingerprint", "kinds"]
