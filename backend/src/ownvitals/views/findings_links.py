"""A finding's trigger groups as the notification hold needs them (ADR 0006 section 11).

Pure: no database, clock or I/O. ``ownvitals.views.findings_read`` loads the groups and the
linked recordings and calls ``trigger_links``.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from ownvitals.ecg.overlay import GroupKey, RecordingKey, estimated_current
from ownvitals.views.findings_models import TriggerLink


def trigger_links(
    linked: Mapping[int, Sequence[RecordingKey]], groups: Sequence[GroupKey]
) -> tuple[TriggerLink, ...]:
    """One link per (grpid, connection): a grpid stored under two connections yields two, so
    the hold judges each (capability, sync, recordings) and either can link it. A grpid with no
    live group yields one link with no connection."""
    connections: dict[int, list[UUID]] = {}
    for group in groups:
        found = connections.setdefault(group.grpid, [])
        if group.connection not in found:
            found.append(group.connection)
    links: list[TriggerLink] = []
    for grpid, recordings in linked.items():
        # A linked recording's connection is always one of its group's; keep it if not.
        known = connections.get(grpid, [])
        extra = [c for c in dict.fromkeys(r.connection for r in recordings) if c not in known]
        for connection in [*known, *extra] or [None]:
            mine = [r for r in recordings if r.connection == connection]
            links.append(
                TriggerLink(
                    grpid=grpid,
                    connection=connection,
                    linked=len(mine),
                    without_estimate=sum(
                        1 for r in mine if not estimated_current(r.estimated, r.fingerprint)
                    ),
                )
            )
    return tuple(links)
