"""The provenance hash of the rule evaluation (#187, PLAN §5): ``fingerprint(inputs)``.

Split from ``derived.evaluate`` (no behaviour change). Pure.
"""

import hashlib

from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.core import SPECS, Provenance
from ownvitals.derived.evaluate_model import RULES_VERSION, Inputs


def _reading_line(key: str, r: Reading) -> str:
    return "|".join(
        (key, str(r.grpid), r.measured_at.isoformat(), r.source_tz, str(r.attrib), str(r.value))
    )


def fingerprint(inputs: Inputs) -> bytes:
    """SHA-256 over the rule versions, ``today``, the switch and every input ``evaluate`` reads,
    in a canonical order (PLAN §5). Any change to any of them changes it."""
    lines: list[str] = [
        RULES_VERSION,
        ",".join(f"{rule}={spec.version}" for rule, spec in sorted(SPECS.items())),
        f"today={inputs.today.isoformat()}",
        f"experimental={inputs.experimental}",
    ]
    for key in sorted(inputs.daily):
        for v in sorted(inputs.daily[key], key=lambda v: v.day):
            prov = inputs.provenance.get(key, {}).get(v.day, Provenance())
            lines.append(
                "|".join(
                    (
                        "daily",
                        key,
                        v.day.isoformat(),
                        v.method.value,
                        str(v.value),
                        str(v.n),
                        str(v.minimum),
                        str(v.maximum),
                        str(v.mad),
                        ",".join(str(g) for g in prov.groups),
                        str(prov.jump),
                    )
                )
            )
    for key in sorted(inputs.readings):
        lines.extend(sorted(_reading_line(key, r) for r in inputs.readings[key]))
    lines.extend(sorted(_reading_line(f"segment:{s.position}", s.reading) for s in inputs.segments))
    q1 = inputs.q1
    lines.append(f"q1|capability|{q1.weight_capability}")
    if q1.connection is not None:
        lines.append(f"q1|connection|{q1.connection.status}")
    lines.extend(sorted(f"q1|device|{d.hash_deviceid}|{d.battery}" for d in q1.devices))
    lines.extend(
        sorted(
            f"q1|stream|{s.stream}|{s.capability}|{s.complete}|{s.importing}" for s in q1.streams
        )
    )
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode())
        digest.update(b"\n")
    return digest.digest()
