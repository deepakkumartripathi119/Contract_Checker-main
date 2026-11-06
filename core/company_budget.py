"""One HTTP budget per company, counted in Redis.

This counts work requests: a policy upload, a contract upload, or a pasted
clause. A status poll does not count. The nginx limit on this machine is
separate and does not see the company id.
"""

from __future__ import annotations

import os
import time


class BudgetExceeded(Exception):
    """The company used its requests for this minute."""


def charge_company(org_id: str, client=None, limit: int | None = None) -> None:
    """Add one request for this company. Do nothing when Redis is not the cache."""
    if client is None and os.getenv("CACHE_BACKEND", "sqlite").lower() != "redis":
        return
    cap = limit if limit is not None else int(os.getenv("COMPANY_HTTP_LIMIT_PER_MINUTE", "60"))
    minute = int(time.time()) // 60
    key = f"cc:budget:{org_id}:{minute}"
    redis_client = client or _client()
    try:
        count = int(redis_client.incr(key))
        if count == 1:
            redis_client.expire(key, 120)
    except Exception:
        return
    if count > cap:
        raise BudgetExceeded("This company has used its requests for this minute.")


def _client():
    import redis

    return redis.Redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
