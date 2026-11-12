"""What a locale's outbox table holds (ADR 0015 section 7): a fixed subject and body per kind, the
body holding ``LINK`` exactly once and no other placeholder. Shared by ``en`` and ``sv``."""

from dataclasses import dataclass
from typing import Final

LINK: Final = "{link}"
# The one placeholder of a locale's unsubscribe footer (ADR 0018; #459).
UNSUBSCRIBE_LINK: Final = "{unsubscribe}"


@dataclass(frozen=True, slots=True)
class Template:
    subject: str
    body: str  # holds ``LINK`` exactly once
