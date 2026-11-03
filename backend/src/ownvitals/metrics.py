"""A small Prometheus registry (pure, no sockets) (PLAN section 4.4).

No client library: counters, gauges and fixed-bucket histograms, rendered in the text
exposition format 0.0.4. The registry is **label-safe by construction**:

* a label name must be on ``ALLOWED_LABELS`` (no user id, email, token, value or timestamp can
  be named);
* every label has a **closed set of values** declared with the metric; any other value raises
  ``LabelError`` instead of becoming a new series (so a user id, an email or a measurement can
  never appear, and the series count is bounded).

Metrics hold counts, ages and states, aggregated across users. This module is pure: it opens
no socket. The scrape endpoint is #167's.
"""

import math
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

ALLOWED_LABELS: Final = frozenset({"stream", "state", "kind", "result", "category", "app"})
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class LabelError(ValueError):
    """A label name or value outside the declared closed vocabulary."""


def _check_label_name(name: str) -> None:
    if name not in ALLOWED_LABELS or not _NAME.match(name):
        raise LabelError(f"label {name!r} is not allowed")


def _escape(value: str) -> str:
    """Label values: backslash, newline and double quote."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(text: str) -> str:
    """HELP text: backslash and newline only (exposition format 0.0.4)."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _fmt(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    return repr(float(value)) if value != int(value) else str(int(value))


@dataclass
class _Metric:
    name: str
    description: str
    kind: str
    labels: Mapping[str, frozenset[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"bad metric name {self.name!r}")
        for label, values in self.labels.items():
            _check_label_name(label)
            if label == "le":
                raise LabelError("le is reserved for histogram buckets")
            if not values:
                raise LabelError(f"label {label!r} has no declared values")

    def _key(self, given: Mapping[str, str]) -> tuple[str, ...]:
        if set(given) != set(self.labels):
            raise LabelError(f"{self.name}: labels must be exactly {sorted(self.labels)}")
        for label, value in given.items():
            if value not in self.labels[label]:
                raise LabelError(f"{self.name}: {label} value is outside its declared set")
        return tuple(given[label] for label in self.labels)

    def _line(self, suffix: str, key: tuple[str, ...], value: float, extra: str = "") -> str:
        pairs = [f'{label}="{_escape(v)}"' for label, v in zip(self.labels, key, strict=True)]
        if extra:
            pairs.append(extra)
        body = "{" + ",".join(pairs) + "}" if pairs else ""
        return f"{self.name}{suffix}{body} {_fmt(value)}"

    def keys(self) -> list[tuple[str, ...]]:
        raise NotImplementedError

    def render(self) -> list[str]:
        raise NotImplementedError


class Counter(_Metric):
    def __init__(
        self, name: str, description: str, labels: Mapping[str, Iterable[str]] | None = None
    ):
        super().__init__(
            name, description, "counter", {k: frozenset(v) for k, v in (labels or {}).items()}
        )
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1, **labels: str) -> None:
        if amount < 0:
            raise ValueError("a counter only goes up")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        with self._lock:
            return [self._line("", k, v) for k, v in sorted(self._values.items())]

    def reset(self) -> None:
        with self._lock:
            self._values.clear()


class Gauge(_Metric):
    def __init__(
        self, name: str, description: str, labels: Mapping[str, Iterable[str]] | None = None
    ):
        super().__init__(
            name, description, "gauge", {k: frozenset(v) for k, v in (labels or {}).items()}
        )
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def get(self, **labels: str) -> float | None:
        return self._values.get(self._key(labels))

    def render(self) -> list[str]:
        with self._lock:
            return [self._line("", k, v) for k, v in sorted(self._values.items())]

    def reset(self) -> None:
        with self._lock:
            self._values.clear()


class Histogram(_Metric):
    def __init__(
        self,
        name: str,
        description: str,
        buckets: Sequence[float],
        labels: Mapping[str, Iterable[str]] | None = None,
    ):
        super().__init__(
            name, description, "histogram", {k: frozenset(v) for k, v in (labels or {}).items()}
        )
        if "le" in self.labels:
            raise LabelError("le is reserved for histogram buckets")
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self.buckets) + 1))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1
            counts[-1] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value

    def count(self, **labels: str) -> int:
        counts = self._counts.get(self._key(labels))
        return counts[-1] if counts else 0

    def render(self) -> list[str]:
        lines: list[str] = []
        with self._lock:
            for key, counts in sorted(self._counts.items()):
                for bound, n in zip(self.buckets, counts, strict=False):
                    lines.append(self._line("_bucket", key, n, f'le="{_fmt(bound)}"'))
                lines.append(self._line("_bucket", key, counts[-1], 'le="+Inf"'))
                lines.append(self._line("_sum", key, self._sums[key]))
                lines.append(self._line("_count", key, counts[-1]))
        return lines

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._sums.clear()


class Registry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}

    def register[M: _Metric](self, metric: M) -> M:
        if metric.name in self._metrics:
            raise ValueError(f"duplicate metric {metric.name}")
        self._metrics[metric.name] = metric
        return metric

    @property
    def metrics(self) -> tuple[_Metric, ...]:
        return tuple(self._metrics.values())

    def render(self) -> str:
        out: list[str] = []
        for metric in self._metrics.values():
            out.append(f"# HELP {metric.name} {_escape_help(metric.description)}")
            out.append(f"# TYPE {metric.name} {metric.kind}")
            out.extend(metric.render())
        return "\n".join(out) + "\n"

    def reset(self) -> None:
        for metric in self._metrics.values():
            metric.reset()  # type: ignore[attr-defined]


REGISTRY: Final = Registry()
