"""The job issuer of ``UserContext`` (ADR 0002 section 1, ADR 0005 section 4).

The claim function returns only ``(job_id, owner_id, lease_version)``: there is no ambient
authority. From that row alone this module

1. asks the account directory whether the owner exists and is not disabled (``Accounts.
   owner_active``; ADR 0005 section 4 puts that question to ``ownvitals.auth``, guard 8: only auth
   reads the account tables). An owner that is not active gets a bare context, and the job fails
   ``account_disabled``;
2. issues a context with ``role="user"`` (a job never carries admin authority, whoever owns it)
   and the owner's connection refs (``Accounts.connection_refs``, read through
   ``ownvitals.connections`` in that owner's own ``user_session``).

Everything else about the job is then read under RLS as the owner (``ownvitals.jobs.runner``).
This is also the issuer for jobs the webhook and the scheduler create (M1): how a job was created
never changes how its context is built.

**The account directory is a seam until #10 and #13** (Architect decision, 2026-10-31, #77):
neither the account tables nor ``ownvitals.auth``'s owner check nor the connection refs exist yet.
The shipped default, ``NO_ACCOUNTS``, fails closed: every owner is inactive (so every job fails
``account_disabled``) and has no connection refs. #10 wires ``ownvitals.auth``'s check and #13
``ownvitals.connections``' refs into the worker; tests pass a fake.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext, issuer_token
from ownvitals.jobs.fence import Lease

Factory = async_sessionmaker[AsyncSession]
Refs = frozenset[tuple[str, UUID]]


@dataclass(frozen=True, slots=True)
class Claimed:
    """One row of ``queue_claim``."""

    job_id: UUID
    owner_id: UUID
    lease_version: int


@dataclass(frozen=True, slots=True)
class Issued:
    lease: Lease
    active: bool  # False: the owner is gone or disabled; the lease's context has no connections


class Accounts(Protocol):
    """What the issuer asks about the owner of a claimed job."""

    async def owner_active(self, factory: Factory, owner_id: UUID) -> bool:
        """Whether the account exists and is not disabled (``ownvitals.auth`` from #10)."""
        ...

    async def connection_refs(self, factory: Factory, ctx: UserContext) -> Refs:
        """The owner's ``(provider, connection id)`` refs, read in the owner's own session
        (``ownvitals.connections`` from #13). ``ctx`` is the owner's bare context."""
        ...


class NoAccounts:
    """The fail-closed default until #10 and #13: nobody is active, nobody has a connection."""

    async def owner_active(self, factory: Factory, owner_id: UUID) -> bool:
        return False

    async def connection_refs(self, factory: Factory, ctx: UserContext) -> Refs:
        return frozenset()


NO_ACCOUNTS: Accounts = NoAccounts()


def owner_context(owner_id: UUID) -> UserContext:
    """A bare context for the owner (role ``user``, nothing else): for the ``account_disabled``
    failure and a hand-back before the context was issued, which touch only the job's own row."""
    return UserContext(user_id=owner_id, role="user", issuer=issuer_token())


async def issue(factory: Factory, claimed: Claimed, accounts: Accounts) -> Issued:
    bare = owner_context(claimed.owner_id)
    if not await accounts.owner_active(factory, claimed.owner_id):
        return Issued(Lease(claimed.job_id, claimed.lease_version, bare), active=False)
    refs = await accounts.connection_refs(factory, bare)
    if not isinstance(refs, frozenset):
        raise TypeError("connection_refs must return a frozenset of (provider, connection id)")
    ctx = UserContext(
        user_id=claimed.owner_id, role="user", connections=refs, issuer=issuer_token()
    )
    return Issued(Lease(claimed.job_id, claimed.lease_version, ctx), active=True)
