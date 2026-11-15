"""The owner's model evaluation: the shape-only summary (ADR 0014 section 9; #527). Pure.

The summary holds, per category, counts and a yes or no, and for the run the model, the prompt
and tool-set versions and the largest ratio of reported to bounded input tokens. **Never** a
prompt, a model answer, an id or a secret. ``ai_eval_ref`` (exactly ``<model>/<prompt
version>/<tool-set version>``, lock 3) is printed only when every category met its threshold,
every case ran and the ratio is not above 1; the runner never writes the setting itself, the
owner does after reading this.

``show_failed`` adds the static labels of the cases that failed (our own constants such as
``grounding-12``, never derived from an answer); it is off unless the owner asks.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ownvitals.ai.eval_model import CATEGORIES, CategoryDef, Outcome, rate_met
from ownvitals.ai.requests import PROMPT_VERSION, eval_ref
from ownvitals.ai.tools_model import TOOLSET_VERSION

RATIO_LIMIT = 1.0


@dataclass(frozen=True, slots=True)
class CategoryResult:
    name: str
    cases: int  # graded cases (a review case is not graded)
    passes: int
    disclosures: int
    review: int
    groups: tuple[tuple[str, int, int, int], ...]  # name, passes, cases, required percent
    met: bool
    failed: tuple[str, ...]


def _result(definition: CategoryDef, outcomes: Sequence[Outcome]) -> CategoryResult:
    mine = [o for o in outcomes if o.case.category == definition.name]
    graded = [o for o in mine if not o.case.review]
    groups: list[tuple[str, int, int, int]] = []
    for group in definition.groups:
        of_group = [o for o in graded if o.case.group == group.name]
        passes = sum(1 for o in of_group if o.verdict is not None and o.verdict.passed)
        groups.append((group.name, passes, len(of_group), group.percent))
    disclosures = sum(1 for o in graded if o.verdict is not None and o.verdict.disclosure)
    met = (
        len(graded) >= definition.min_cases
        and all(rate_met(p, c, pct) for _, p, c, pct in groups)
        and not (definition.no_disclosure and disclosures)
    )
    failed = tuple(o.case.label for o in graded if o.verdict is None or not o.verdict.passed)
    return CategoryResult(
        definition.name,
        len(graded),
        sum(g[1] for g in groups),
        disclosures,
        len(mine) - len(graded),
        tuple(groups),
        met,
        failed,
    )


def summarise(outcomes: Sequence[Outcome]) -> list[CategoryResult]:
    return [_result(definition, outcomes) for definition in CATEGORIES]


def passed(results: Sequence[CategoryResult], ratio: float | None, aborted: str | None) -> bool:
    """Every category met, no abort, at least one call and the ratio not above 1."""
    return (
        aborted is None
        and ratio is not None
        and ratio <= RATIO_LIMIT
        and len(results) == len(CATEGORIES)
        and all(r.met for r in results)
    )


def render(
    results: Sequence[CategoryResult],
    *,
    model: str,
    ratio: float | None,
    calls: int,
    spend_microusd: int,
    aborted: str | None,
    show_failed: bool = False,
    extra: Mapping[str, str] | None = None,
) -> list[str]:
    """The lines to print."""
    lines = [f"ownvitals ai evaluation: model={model} prompt_version={PROMPT_VERSION} "
             f"toolset_version={TOOLSET_VERSION}"]  # fmt: skip
    for r in results:
        detail = ",".join(f"{n}:{p}/{c}>={pct}" for n, p, c, pct in r.groups)
        lines.append(
            f"category={r.name} cases={r.cases} passes={r.passes} groups={detail} "
            f"disclosures={r.disclosures} review={r.review} met={'yes' if r.met else 'no'}"
        )
        if show_failed and r.failed:
            lines.append(f"failed {r.name}: {' '.join(r.failed)}")
    shown = "none" if ratio is None else f"{ratio:.3f}"
    lines.append(
        f"calls={calls} input_ratio_max={shown} spend_microusd={spend_microusd} "
        f"aborted={aborted or 'no'}"
    )
    ok = passed(results, ratio, aborted)
    lines.append(f"result={'PASS' if ok else 'FAIL'}")
    if ok:
        lines.append(f"ai_eval_ref={eval_ref(model)}")
    for key, value in (extra or {}).items():
        lines.append(f"{key}={value}")
    return lines
