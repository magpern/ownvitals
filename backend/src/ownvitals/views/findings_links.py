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
    connection: dict[int, UUID] = {}
    for group in groups:
        connection.setdefault(group.grpid, group.connection)
    return tuple(
        TriggerLink(
            grpid=grpid,
            connection=connection.get(grpid),
            linked=len(recordings),
            without_estimate=sum(
                1 for r in recordings if not estimated_current(r.estimated, r.fingerprint)
            ),
        )
        for grpid, recordings in linked.items()
    )
