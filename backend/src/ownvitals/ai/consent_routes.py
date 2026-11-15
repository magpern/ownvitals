"""``/api/ai/consent``: the AI consent text, giving consent and withdrawing it (ADR 0014 section
3, PLAN section 7.4; #206).

- ``GET``: whether AI insights are available (the effective ``ai_insights`` flag), the user's
  consent state, and the current consent text in the requested language with its version and
  SHA-256 (what a consent must name). A full session.
- ``POST``: give consent to exactly that text (``version``, ``locale``, ``sha256``, and
  ``agree`` true: the unticked box). Needs a **step-up** (it starts sending health data to a
  third party; ADR 0003 section 4). While the flag is off: 409 ``ai_not_enabled``. Any other
  text: 409 ``ai_consent_outdated``.
- ``DELETE``: withdraw. A full session only, no step-up, and also while the flag is off. Stops
  AI processing at once and deletes the conversation history (``ai.consent.withdraw``).

Every write goes through the Origin and CSRF checks of the auth middleware. Only the caller's
own rows are read or written, under forced RLS. A response carries codes, never sentences,
except the consent text itself, which is the document agreed to.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ownvitals.ai import consent, consent_text
from ownvitals.auth.deps import require_stepup, require_user
from ownvitals.context import UserContext

router = APIRouter(prefix="/api/ai")

Locale = Annotated[str, Query(pattern="^[a-z]{2}$")]


class ConsentTextOut(BaseModel):
    version: int
    locale: str
    text: str
    sha256: str  # hex


class ConsentStateOut(BaseModel):
    state: Literal["none", "given", "outdated"]
    version: int | None
    given_at: datetime | None


class ConsentOut(BaseModel):
    enabled: bool  # the effective ai_insights flag
    consent: ConsentStateOut
    consent_text: ConsentTextOut


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=10_000)
    locale: str = Field(pattern="^[a-z]{2}$")
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    agree: Literal[True]


class WithdrawnOut(BaseModel):
    withdrawn: bool
    cancelled_jobs: int
    deleted_messages: int


def _state(found: consent.ConsentState | None) -> ConsentStateOut:
    if found is None:
        return ConsentStateOut(state="none", version=None, given_at=None)
    return ConsentStateOut(
        state="given" if found.current else "outdated",
        version=found.version,
        given_at=found.given_at,
    )


def _text(locale: str | None) -> ConsentTextOut:
    chosen, body = consent_text.text_for(locale)
    return ConsentTextOut(
        version=consent_text.CONSENT_VERSION,
        locale=chosen,
        text=body,
        sha256=consent_text.fingerprint(body).hex(),
    )


@router.get("/consent", response_model=ConsentOut)
async def get_consent(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    locale: Locale = consent_text.DEFAULT_LOCALE,
) -> ConsentOut:
    factory, settings = request.app.state.session_factory, request.app.state.settings
    return ConsentOut(
        enabled=await consent.feature_on(factory, ctx, settings),
        consent=_state(await consent.read_state(factory, ctx)),
        consent_text=_text(locale),
    )


@router.post("/consent", response_model=ConsentOut)
async def give_consent(
    body: ConsentIn, request: Request, ctx: Annotated[UserContext, Depends(require_stepup)]
) -> ConsentOut:
    factory, settings = request.app.state.session_factory, request.app.state.settings
    try:
        given = await consent.give(
            factory,
            ctx,
            settings,
            version=body.version,
            locale=body.locale,
            digest=bytes.fromhex(body.sha256),
        )
    except consent.AiNotEnabled as refused:
        raise HTTPException(status_code=409, detail={"reason": "ai_not_enabled"}) from refused
    except consent.ConsentMismatch as refused:
        raise HTTPException(status_code=409, detail={"reason": "ai_consent_outdated"}) from refused
    return ConsentOut(enabled=True, consent=_state(given), consent_text=_text(body.locale))


@router.delete("/consent", response_model=WithdrawnOut)
async def withdraw_consent(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> WithdrawnOut:
    done = await consent.withdraw(request.app.state.session_factory, ctx)
    return WithdrawnOut(
        withdrawn=done.closed,
        cancelled_jobs=done.cancelled_jobs,
        deleted_messages=done.deleted.get("ai_messages", 0),
    )
