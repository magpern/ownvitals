"""The chat assistant's conversation-creation limit (ADR 0014 section 8; #352): 30 new
conversations per hour per user, every accepted one counted, the bucket key an HMAC of the user's
id like the message limit (``auth.ratelimit.AI_CHAT_ACCOUNT``). Raises ``RateLimited`` (429 with
``Retry-After``) above it."""

from fastapi import Request

from ownvitals.auth.flowtools import reserve
from ownvitals.auth.ratelimit import Bucket, Limit
from ownvitals.context import UserContext

AI_CONVERSATION_ACCOUNT = Limit("ai-conversation", "account", 30, 3600)


async def reserve_ai_conversation(request: Request, ctx: UserContext) -> None:
    await reserve(request, [Bucket(AI_CONVERSATION_ACCOUNT, str(ctx.user_id))])
