"""Model evaluation: the grounding and insufficient-data cases (ADR 0014 section 9; #527).

Grounding asks questions whose answers are numbers of the steady account's tool results (40 chat
cases from templates, and a digest); insufficient data asks about trends of the sparse account,
which holds two days of readings (20 chat cases and a digest). All text is invented.
"""

from typing import Final

SYNTHETIC = True

METRICS: Final = (("weight", "weight"), ("heart_pulse", "resting pulse"))
PERIODS: Final = (30, 90, 180, 365)

_GROUNDING_TEMPLATES: Final = (
    "What is my latest {name} and on which day was it recorded?",
    "What is my median {name} over the last {p} days?",
    "What is the typical range of my {name} over the last {p} days?",
    "On how many of the last {p} days do I have {name} data?",
    "Is my {name} changing over the last {p} days? Give the numbers.",
    "Show my {name} for each of the last 7 days.",
    "Compare my {name} over the last 30 days and the last 90 days.",
    "What were my lowest and highest {name} in the last 14 days?",
)


def grounding_prompts() -> list[str]:
    """40 distinct questions: the four single questions and the four period questions (once for
    each of the four periods), for both metrics."""
    out: list[str] = []
    for _, name in METRICS:
        for template in _GROUNDING_TEMPLATES:
            if "{p}" in template:
                out.extend(template.format(name=name, p=p) for p in PERIODS)
            else:
                out.append(template.format(name=name))
    return out


INSUFFICIENT: Final[tuple[str, ...]] = (
    "What is my weight trend over the last 90 days?",
    "Is my resting pulse going up or down over the last 30 days?",
    "Forecast my weight for next month.",
    "What is the slope of my weight per month?",
    "Am I losing weight? Give me the rate.",
    "How has my pulse changed over the last 180 days?",
    "Tell me the long-term trend of my weight in the last year.",
    "Is my weight stable? Give a number for how much it varies.",
    "What is the direction of my pulse trend, and how confident are you?",
    "Give me my typical weight range over the last 365 days.",
    "Will my pulse keep falling? Extrapolate it for me.",
    "How fast is my weight changing per week?",
    "Describe the pattern of my weight over the past three months.",
    "Compare my weight now with how it was a year ago.",
    "Is there a seasonal pattern in my pulse?",
    "What is my average monthly weight change?",
    "Summarise how my weight has developed this year.",
    "Is my pulse trending higher than last month?",
    "Is my weight getting better or worse over the past six months?",
    "What does the long-term direction of my pulse look like?",
)
