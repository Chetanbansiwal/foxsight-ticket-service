"""Per-camera severity overrides, applied where the ticket is created.

event-management owns this table and resolves it for camera-status alarms
(offline, tamper, video-loss) before it calls us. Analytics alarms never pass
through that path: analytics-service posts straight to POST /api/tickets, so
its severity arrived exactly as the rule (or the built-in default) set it and
the operator's per-camera override was ignored. The comment on the cache in
event-management says "every camera status message AND every analytics alert
resolves a severity" — only the first half was true.

Resolving here rather than adding another producer-side call is deliberate:
every alarm ticket funnels through ticket creation, and this is already where
alarm_type is derived (see alarm_type_map), so the (camera_id, alarm_type) key
this is looked up by is settled at exactly this point.

Applying it twice is harmless. For a camera-status alarm event-management has
already resolved the same key to the same value, and an override maps a key to
one severity regardless of the incoming default — so a second pass is a no-op
rather than a compounding change.

Cached in memory with a short TTL for the same reason event-management caches:
this sits on the alarm path, and a database round trip per ticket would put
Postgres in the middle of it. An absent entry means "use what the caller sent",
so a site that never sets an override behaves exactly as before.
"""
import os
import time
from typing import Dict, Optional, Tuple

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import AlarmSeverityOverride

logger = structlog.get_logger()

CACHE_TTL_S = float(os.getenv("SEVERITY_CACHE_TTL_S", "60"))

_OVERRIDES: Dict[Tuple[int, str], str] = {}
_LOADED_AT = 0.0


async def load_overrides(db: AsyncSession, force: bool = False) -> int:
    """Refresh the cache. Returns how many overrides are held."""
    global _OVERRIDES, _LOADED_AT
    if not force and (time.time() - _LOADED_AT) < CACHE_TTL_S:
        return len(_OVERRIDES)
    try:
        rows = (await db.execute(select(AlarmSeverityOverride))).scalars().all()
        _OVERRIDES = {
            (int(r.camera_id), r.alarm_type): (r.severity or "").lower()
            for r in rows if r.severity and r.camera_id is not None
        }
        _LOADED_AT = time.time()
    except Exception as e:
        # Never fatal. Falling back to the caller's severity is the previous
        # behaviour; failing the ticket would lose an alarm outright, which is a
        # far worse trade for a configuration lookup.
        logger.warning("Could not load severity overrides", error=str(e))
    return len(_OVERRIDES)


async def resolve_severity(
    db: AsyncSession,
    camera_id: Optional[int],
    alarm_type: Optional[str],
    default: Optional[str],
) -> Optional[str]:
    """The severity for this alarm on this camera, else `default` unchanged.

    Casing is preserved from the override (stored lowercase) and otherwise from
    the caller — normalising the caller's value here would change tickets that
    have no override, which is not this function's business.
    """
    if camera_id is None or not alarm_type:
        return default
    await load_overrides(db)
    return _OVERRIDES.get((int(camera_id), alarm_type)) or default


def _cache_for_test() -> Dict[Tuple[int, str], str]:
    return _OVERRIDES
