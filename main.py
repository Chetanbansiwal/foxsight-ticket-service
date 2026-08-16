"""
Ticket Service

Centralized ticket management service for Phase 2 analytics platform.
Handles alert-to-ticket conversion, ticket lifecycle, and SLA tracking.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
import time as _time
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Request, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, update, func, text, delete, case
from sqlalchemy.orm import selectinload
import structlog

# Import shared modules - using installed vms-shared package
from database import db_manager, get_db, get_db_session, get_redis
from models import (
    Ticket, TicketComment, TicketStateHistory, AlarmEventLink,
    NotificationLog, AnalyticsProvider, User, Camera, Zone, UserZoneAccess,
    OrgRole, EscalationPolicy, EscalationLevel, EscalationRecipient, EscalationAction,
    SLAPolicy, ShiftRoster,
    TicketStatus
)
import json
import uuid as _uuid
from types import SimpleNamespace
from auth import get_current_user_flexible, get_user_from_headers
from zone_scoping import resolve_user_zone_ids, scope_by_camera_id

# Configure logging
logger = structlog.get_logger()


async def _ensure_alarm_schema():
    """WS0 self-migration — additive + idempotent, safe every boot (mirrors
    camera-management's _ensure_zone_schema). create_all makes the
    alarm_event_link table, but NOT the new `tickets` columns nor the NOT NULL
    relaxations on an already-existing install — so a fresh install or
    `foxsight update` self-migrates regardless of the box's migrate script."""
    try:
        async with get_db_session() as s:
            await s.execute(text("""
                ALTER TABLE tickets
                    ADD COLUMN IF NOT EXISTS alarm_type       VARCHAR(50),
                    ADD COLUMN IF NOT EXISTS primary_event_id INTEGER,
                    ADD COLUMN IF NOT EXISTS is_latched       BOOLEAN DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS latch_cleared_at DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS last_occurred_at DOUBLE PRECISION
            """))
            # Backfill: existing rows have no occurrence stamp, and a NULL would
            # send consumers straight back to created_at — the bug this column
            # exists to end. The newest linked event is the best evidence we
            # have of when each alarm last fired; tickets with no links fall
            # back to their own created_at.
            await s.execute(text("""
                UPDATE tickets t SET last_occurred_at = COALESCE(
                    (SELECT MAX(l.created_at) FROM alarm_event_link l
                      WHERE l.ticket_id = t.id),
                    t.created_at)
                WHERE t.last_occurred_at IS NULL AND t.alarm_type IS NOT NULL
            """))
            await s.execute(text("ALTER TABLE tickets ALTER COLUMN camera_id DROP NOT NULL"))
            await s.execute(text("ALTER TABLE tickets ALTER COLUMN provider_id DROP NOT NULL"))
            await s.execute(text("CREATE INDEX IF NOT EXISTS ix_tickets_alarm_type ON tickets(alarm_type)"))
            await s.execute(text("CREATE INDEX IF NOT EXISTS ix_tickets_is_latched ON tickets(is_latched)"))
            await s.execute(text("""
                CREATE TABLE IF NOT EXISTS alarm_event_link (
                    ticket_id  VARCHAR(36) NOT NULL REFERENCES tickets(id),
                    event_id   INTEGER     NOT NULL REFERENCES events(id),
                    created_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (ticket_id, event_id)
                )
            """))
        logger.info("WS0 alarm schema ensured (self-migration)")
    except Exception as e:
        logger.warning("Alarm self-migration skipped/failed", error=str(e))


async def _ensure_escalation_schema():
    """WS1 self-migration — additive + idempotent. The escalation_* tables are
    created by Base.metadata.create_all (new tables); this only adds the new
    escalation columns to the existing tickets table + their index."""
    try:
        async with get_db_session() as s:
            await s.execute(text("""
                ALTER TABLE tickets
                    ADD COLUMN IF NOT EXISTS escalation_policy_id VARCHAR(36),
                    ADD COLUMN IF NOT EXISTS escalation_level     INTEGER DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS escalated_at         DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS next_escalation_at   DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS acknowledged_at      DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS acknowledged_by      INTEGER
            """))
            await s.execute(text("CREATE INDEX IF NOT EXISTS ix_tickets_next_escalation_at ON tickets(next_escalation_at)"))
        logger.info("WS1 escalation schema ensured (self-migration)")
    except Exception as e:
        logger.warning("Escalation self-migration skipped/failed", error=str(e))


# ============================================================================
# WS1 — Escalation engine (see compliances/WS1_ESCALATION_RBAC_PLAN.md).
#   alarm fired → match policy → schedule → fire levels over time (resolving
#   zone-scoped recipients) → halt on ack/resolve. In-app delivery is live now
#   via the event WS bus (notify:user channel → event-management send_to_user);
#   email/SMS are written to notification_logs as 'pending' for WS2 to send.
# ============================================================================
NOTIFY_USER_CHANNEL = "notify:user"
ESCALATION_TICK_SECONDS = int(os.getenv("ESCALATION_TICK_SECONDS", "15"))
_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

# The channels a recipient may be given. `webhook` used to be offered here and
# was undeliverable by construction: delivery resolves an address from the
# user's verified NotificationChannel rows, and user-management only ever
# accepts email/sms/whatsapp/push as a channel type — so a webhook row could
# never resolve a destination and failed on every single attempt. A per-level
# `webhook` ACTION is the real feature and is wired separately
# (_run_response_action). Anything not in this set is dropped at write time so
# a stale client cannot re-introduce a silent black hole.
VALID_CHANNELS = ("push", "email", "sms", "whatsapp")
# Channels whose out-of-band sender is not integrated yet. They are accepted and
# logged so the audit trail is honest about what was asked for, but the UI must
# not offer them as if they deliver. See _dispatch_one in user-management.
UNWIRED_CHANNELS = ("sms", "whatsapp")


def _clean_channels(raw) -> list:
    """Normalize a recipient's channel list: known channels only, order-stable,
    de-duplicated, and never empty (in-app push is the floor — a recipient with
    no channel at all would be a row that notifies nobody)."""
    out = []
    for c in (raw or []):
        c = str(c).strip().lower()
        if c in VALID_CHANNELS and c not in out:
            out.append(c)
    return out or ["push"]


def _stamp_ack(ticket, user_id, now):
    """First response / acknowledge stamps (WS1) — also HALTS escalation. Idempotent."""
    if ticket.acknowledged_at is None:
        ticket.acknowledged_at = now
        ticket.acknowledged_by = user_id
    if ticket.first_response_at is None:
        ticket.first_response_at = now
        ticket.first_response_time_seconds = int(now - (ticket.created_at or now))
    ticket.next_escalation_at = None
    ticket.updated_at = now


def _status_note(json_data):
    """The operator's note for a status change, under either spelling.

    The web client has always sent `reason` (resolveTicket / closeTicket both
    take one), while this endpoint only ever read `comment` — so the stated
    reason for resolving or closing a ticket was accepted and silently dropped.
    `reason` wins because it is the live caller. Blank or whitespace-only text
    counts as absent rather than becoming an empty comment.
    """
    note = json_data.get('reason') or json_data.get('comment')
    if note is None:
        return None
    text = str(note).strip()
    return text or None


async def _alarm_zone_path(db, ticket):
    """Materialized-path of the alarm's zone (via camera). None if camera-less/unzoned."""
    if ticket.camera_id is None:
        return None
    zid = (await db.execute(select(Camera.zone_id).where(Camera.id == ticket.camera_id))).scalar_one_or_none()
    if not zid:
        return None
    return (await db.execute(select(Zone.path).where(Zone.id == zid))).scalar_one_or_none()


async def _match_policy(db, ticket):
    """Best enabled escalation policy for a ticket: org (or NULL-org) ∧ event-type
    ∧ min-severity ∧ zone-subtree. Highest priority wins."""
    q = select(EscalationPolicy).where(
        EscalationPolicy.enabled == True,
        or_(EscalationPolicy.organization_id == ticket.organization_id,
            EscalationPolicy.organization_id.is_(None)),
    ).order_by(EscalationPolicy.priority.desc())
    policies = (await db.execute(q)).scalars().all()
    if not policies:
        return None
    alarm_path = await _alarm_zone_path(db, ticket)
    tsev = _SEV_RANK.get((ticket.severity or "").lower(), 0)
    for p in policies:
        if p.match_event_types and (ticket.alarm_type or "") not in p.match_event_types:
            continue
        if p.match_severity and tsev < _SEV_RANK.get(p.match_severity.lower(), 0):
            continue
        if p.match_zone_id:
            pzpath = (await db.execute(select(Zone.path).where(Zone.id == p.match_zone_id))).scalar_one_or_none()
            if not (alarm_path and pzpath and alarm_path.startswith(pzpath)):
                continue
        # WS6: time-scoped policies (e.g. a night-shift matrix) only match in-window.
        aw = p.active_window or {}
        if aw and not _in_time_window(aw.get("days"), aw.get("start"), aw.get("end"), aw.get("tz", "UTC")):
            continue
        return p
    return None


def _in_time_window(days, start, end, tz, now_ts=None):
    """WS6: is 'now' (in tz) inside [start,end] on an allowed weekday? Handles
    midnight-wrap (start>end); for a wrapped window the post-midnight tail counts
    against the weekday the shift STARTED. days = [0..6] (Mon..Sun) or falsy=any."""
    from datetime import datetime, time as _dtime
    ts = now_ts if now_ts is not None else _time.time()
    try:
        from zoneinfo import ZoneInfo
        now = datetime.fromtimestamp(ts, ZoneInfo(tz or "UTC"))
    except Exception:
        now = datetime.utcfromtimestamp(ts)
    eff_day = now.weekday()
    if start and end:
        def _p(t):
            h, m = str(t).split(":")
            return _dtime(int(h), int(m))
        s, e, cur = _p(start), _p(end), now.time()
        if s <= e:
            if not (s <= cur < e):
                return False
        else:  # wraps midnight
            if not (cur >= s or cur < e):
                return False
            if cur < e:
                eff_day = (now.weekday() - 1) % 7
    if days:
        return eff_day in days
    return True


async def _oncall_user_for(db, ticket, role_name):
    """WS6: the on-call user id for this role right now, from the shift roster
    (scoped to the alarm's zone subtree), or None if no shift covers now."""
    rosters = (await db.execute(
        select(ShiftRoster).where(ShiftRoster.role_name == role_name))).scalars().all()
    if not rosters:
        return None
    alarm_path = await _alarm_zone_path(db, ticket)
    for r in rosters:
        if r.zone_id:
            zpath = (await db.execute(select(Zone.path).where(Zone.id == r.zone_id))).scalar_one_or_none()
            if not (alarm_path and zpath and alarm_path.startswith(zpath)):
                continue
        if _in_time_window(r.weekdays, r.start_time, r.end_time, r.tz or "UTC"):
            return r.on_call_user_id
    return None


async def _recipients_for_role(db, ticket, role_id):
    """Users holding the given org_role who can see the alarm's zone: global grant,
    a grant covering the alarm's zone (ancestor-or-self), or NO zone grants at all
    (treated as unrestricted, mirroring admin scoping). Camera-less alarms → only
    global/unrestricted holders."""
    role = (await db.execute(select(OrgRole).where(OrgRole.id == role_id))).scalar_one_or_none()
    if not role:
        return []
    role_users = (await db.execute(
        select(User).where(User.role == role.name, User.is_active == True))).scalars().all()
    if not role_users:
        return []
    uids = [u.id for u in role_users]
    alarm_path = await _alarm_zone_path(db, ticket)
    rows = (await db.execute(
        select(UserZoneAccess.user_id, UserZoneAccess.is_global, Zone.path)
        .select_from(UserZoneAccess).outerjoin(Zone, Zone.id == UserZoneAccess.zone_id)
        .where(UserZoneAccess.user_id.in_(uids)))).all()
    grants = {}
    for uid, is_global, gpath in rows:
        grants.setdefault(uid, []).append((is_global, gpath))
    out = []
    for u in role_users:
        g = grants.get(u.id)
        if not g:
            out.append(u)                                   # no grants → unrestricted
        elif any(isg for isg, _ in g):
            out.append(u)                                   # global grant
        elif alarm_path and any(gp and alarm_path.startswith(gp) for _, gp in g):
            out.append(u)                                   # covers the alarm's zone

    # WS6 roster: if a shift covers now for this role (+ the alarm's zone),
    # notify the on-call user specifically instead of everyone holding the role.
    oncall = await _oncall_user_for(db, ticket, role.name)
    if oncall is not None:
        oc = next((u for u in out if u.id == oncall), None)
        if oc:
            return [oc]
        ocu = (await db.execute(
            select(User).where(User.id == oncall, User.is_active == True))).scalar_one_or_none()
        if ocu:
            return [ocu]
    return out


CAMERA_MANAGEMENT_URL = os.getenv("SERVICE_CAMERA_MANAGEMENT_URL", "http://camera-management:8000")


async def _run_response_action(action, ticket) -> None:
    """Execute a programmed response action (WS4, clause 50.8). Best-effort —
    swallows all errors so escalation never stalls on an unreachable relay/URL.
      - actuate_relay: params {output_token, state?} → drive the ticket camera's
        ONVIF relay output via camera-management (service-identity headers).
      - webhook:       params {url} → POST a compact ticket summary."""
    import httpx
    params = action.params or {}
    try:
        if action.action_type == "actuate_relay":
            token = params.get("output_token") or params.get("token")
            if not token or ticket.camera_id is None:
                logger.warning("actuate_relay skipped: missing output_token or camera", ticket_id=ticket.id)
                return
            url = f"{CAMERA_MANAGEMENT_URL}/cameras/{ticket.camera_id}/relay-outputs/{token}"
            headers = {"X-User-ID": "0", "X-User-Role": "administrator", "X-User-Name": "escalation-engine"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json={"state": params.get("state", "active")}, headers=headers)
                logger.info("actuate_relay fired", ticket_id=ticket.id, camera_id=ticket.camera_id,
                            token=token, status=r.status_code)
        elif action.action_type == "webhook":
            url = params.get("url")
            if not url:
                return
            payload = {
                "ticket_id": ticket.id, "ticket_number": ticket.ticket_number,
                "title": ticket.title, "severity": ticket.severity,
                "alarm_type": ticket.alarm_type, "camera_id": ticket.camera_id,
                "escalation_level": ticket.escalation_level,
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
                logger.info("webhook fired", ticket_id=ticket.id, url=url, status=r.status_code)
    except Exception as e:
        logger.warning("response action failed", action_type=action.action_type,
                       ticket_id=ticket.id, error=str(e))


async def _fire_level(db, ticket, level, now):
    """Resolve recipients + write notification_logs + in-app push + run actions.
    Returns the set of notified user ids."""
    recips = (await db.execute(
        select(EscalationRecipient).where(EscalationRecipient.level_id == level.id))).scalars().all()
    notified = set()
    # Who asked for the IN-APP channel specifically.
    #
    # This used to be `notified` — every resolved recipient — so the Redis
    # publish below went out to everyone the level named regardless of which
    # channels were ticked. Un-ticking "push" changed the notification_logs
    # rows and nothing else: the operator still got the toast. The channel
    # picker looked like a control over delivery and was, for the only channel
    # that actually delivered, decoration.
    push_targets = set()
    for r in recips:
        users = []
        if r.recipient_type == "zone_role" and r.role_id:
            users = await _recipients_for_role(db, ticket, r.role_id)
        elif r.recipient_type == "user" and r.user_id:
            u = (await db.execute(select(User).where(User.id == r.user_id))).scalar_one_or_none()
            if u:
                users = [u]
        # 'external' recipients (a bare address, no user row) have no delivery
        # path: notification_logs.user_id is NOT NULL, so there is nowhere to
        # record the attempt. Skipped — and no longer offered by the UI.
        channels = _clean_channels(r.channels)
        for u in users:
            for ch in channels:
                db.add(NotificationLog(
                    id=str(_uuid.uuid4()), ticket_id=ticket.id, user_id=u.id,
                    channel_type=ch, template_name="escalation",
                    status=("sent" if ch == "push" else "pending"),
                    sent_at=(now if ch == "push" else None), created_at=now))
            if "push" in channels:
                push_targets.add(u.id)
            notified.add(u.id)

    for a in (await db.execute(
            select(EscalationAction).where(EscalationAction.level_id == level.id))).scalars().all():
        if a.action_type == "auto_assign" and notified and not ticket.assigned_to_user_id:
            ticket.assigned_to_user_id = min(notified)
            ticket.assigned_at = now
        elif a.action_type in ("actuate_relay", "webhook"):
            # WS4 (clause 50.8): programmed response — drive a camera relay
            # output or hit a webhook when this level fires. Best-effort: a
            # dead relay/URL must not stall the escalation loop.
            await _run_response_action(a, ticket)

    if notified:
        try:
            r = await get_redis()
            await r.publish(NOTIFY_USER_CHANNEL, json.dumps({
                # Only the people who asked for in-app get the addressed copy…
                "user_ids": sorted(push_targets),
                # …but the room-wide "an escalation is running" copy is about
                # awareness, not delivery, so it counts everyone the level
                # named. An email-only level still has to be visible to whoever
                # is actually sitting in the control room.
                "recipient_count": len(notified),
                "ticket_id": ticket.id, "ticket_number": ticket.ticket_number,
                "title": ticket.title, "severity": ticket.severity,
                "level": ticket.escalation_level, "alarm_type": ticket.alarm_type,
            }))
        except Exception as e:
            logger.warning("notify:user publish failed", error=str(e))

    db.add(TicketStateHistory(
        id=str(_uuid.uuid4()), ticket_id=ticket.id,
        from_status=ticket.status, to_status=ticket.status, changed_by_user_id=1,
        reason=f"Escalation L{ticket.escalation_level} fired → {len(notified)} recipient(s)",
        changed_at=now))
    logger.info("Escalation level fired", ticket_id=ticket.id,
                level=ticket.escalation_level, recipients=len(notified))
    return notified


async def _start_escalation(db, ticket):
    """On a new alarm: match a policy, set level 1, schedule the next level, fire L1.
    No-op if no policy matches. Best-effort — never breaks alarm creation."""
    try:
        policy = await _match_policy(db, ticket)
        if not policy:
            return
        levels = sorted(
            (await db.execute(select(EscalationLevel).where(EscalationLevel.policy_id == policy.id))).scalars().all(),
            key=lambda l: l.level_no)
        if not levels:
            return
        now = _time.time()
        ticket.escalation_policy_id = policy.id
        ticket.escalation_level = levels[0].level_no
        ticket.escalated_at = now
        ticket.next_escalation_at = (now + (levels[1].wait_seconds or 0)) if len(levels) > 1 else None
        await _fire_level(db, ticket, levels[0], now)
    except Exception as e:
        logger.warning("start_escalation failed", ticket_id=getattr(ticket, "id", None), error=str(e))


async def _escalation_scheduler_loop():
    """Advance unacknowledged tickets to their next level when due (~ESCALATION_TICK_SECONDS)."""
    await asyncio.sleep(10)  # let startup settle
    while True:
        try:
            now = _time.time()
            async with db_manager.get_session() as db:
                due = (await db.execute(select(Ticket).where(
                    Ticket.next_escalation_at.is_not(None),
                    Ticket.next_escalation_at <= now,
                    Ticket.acknowledged_at.is_(None),
                    Ticket.status.in_(("open", "assigned", "in_progress")),
                ))).scalars().all()
                for ticket in due:
                    levels = sorted(
                        (await db.execute(select(EscalationLevel).where(
                            EscalationLevel.policy_id == ticket.escalation_policy_id))).scalars().all(),
                        key=lambda l: l.level_no)
                    cur = next((i for i, l in enumerate(levels) if l.level_no == ticket.escalation_level), -1)
                    nxt = levels[cur + 1] if 0 <= cur < len(levels) - 1 else None
                    if not nxt:
                        ticket.next_escalation_at = None
                        continue
                    ticket.escalation_level = nxt.level_no
                    ticket.escalated_at = now
                    await _fire_level(db, ticket, nxt, now)
                    after = levels[cur + 2] if cur + 2 < len(levels) else None
                    ticket.next_escalation_at = (now + (after.wait_seconds or 0)) if after else None
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Escalation scheduler error", error=str(e))
        await asyncio.sleep(ESCALATION_TICK_SECONDS)


SLA_TICK_SECONDS = int(os.getenv("SLA_TICK_SECONDS", "60"))


async def _sla_breach_loop():
    """WS6: flag tickets that blow their severity's SLA. For each active,
    not-yet-breached ticket whose severity has an SLAPolicy (org-specific, else
    the NULL-org default), set sla_breach when it exceeds the resolve deadline,
    or the ack deadline while still unacknowledged. Bounded batch per tick."""
    await asyncio.sleep(20)
    while True:
        try:
            now = _time.time()
            async with db_manager.get_session() as db:
                policies = (await db.execute(select(SLAPolicy))).scalars().all()
                if policies:
                    bysev = {}
                    for p in policies:
                        bysev[(p.organization_id, (p.severity or "").lower())] = p
                    # Scope to alarm/incident tickets (alarm_type set) — SLA is an
                    # incident-response deadline, not something to hang on every
                    # analytics detection; this also bounds the scan.
                    tickets = (await db.execute(select(Ticket).where(
                        Ticket.status.in_(("open", "assigned", "in_progress")),
                        Ticket.sla_breach.isnot(True),
                        Ticket.alarm_type.isnot(None),
                    ).order_by(Ticket.created_at.desc()).limit(500))).scalars().all()
                    changed = 0
                    for t in tickets:
                        sev = (t.severity or "").lower()
                        pol = bysev.get((t.organization_id, sev)) or bysev.get((None, sev))
                        if not pol:
                            continue
                        age = now - (t.created_at or now)
                        reason = None
                        if pol.resolve_seconds and age > pol.resolve_seconds:
                            reason = f"Resolution SLA breached (> {pol.resolve_seconds}s)"
                        elif pol.ack_seconds and not t.acknowledged_at and age > pol.ack_seconds:
                            reason = f"Acknowledgement SLA breached (> {pol.ack_seconds}s)"
                        if reason:
                            t.sla_breach = True
                            t.sla_breach_reason = reason
                            changed += 1
                    if changed:
                        await db.commit()
                        logger.info("SLA breaches flagged", count=changed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("SLA breach loop error", error=str(e))
        await asyncio.sleep(SLA_TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    logger.info("Starting Ticket Service...")
    await db_manager.initialize()
    await _ensure_alarm_schema()
    await _ensure_escalation_schema()
    # WS1: the escalation scheduler — ticket-service's first background worker.
    escalation_task = asyncio.create_task(_escalation_scheduler_loop())
    # WS6: SLA-breach detector.
    sla_task = asyncio.create_task(_sla_breach_loop())
    logger.info("Ticket Service started successfully")

    yield

    # Shutdown
    logger.info("Shutting down Ticket Service...")
    escalation_task.cancel()
    try:
        await escalation_task
    except (asyncio.CancelledError, Exception):
        pass
    await db_manager.cleanup()
    logger.info("Ticket Service shutdown complete")

app = FastAPI(
    title="Ticket Service",
    description="Centralized ticket management for analytics alerts",
    version="1.0.0",
    lifespan=lifespan
)

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        from sqlalchemy import text
        async with db_manager.get_session() as session:
            await session.execute(text("SELECT 1"))

        return {
            "service": "ticket-service",
            "status": "healthy",
            "timestamp": _time.time()
        }
    except Exception as e:
        logger.error("Health check failed", error=str(e))
        return {
            "service": "ticket-service",
            "status": "unhealthy",
            "error": str(e)
        }


# ============================================================================
# TICKET CRUD ENDPOINTS
# ============================================================================

@app.post("/api/tickets")
async def create_ticket(
    request: Request,
    caller: Optional[User] = Depends(get_user_from_headers),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new ticket from an analytics alert.

    This is typically called by providers when they generate alerts.

    Body:
    {
        "title": "Person detected in restricted zone",
        "description": "Alert description",
        "severity": "high",
        "camera_id": 1,
        "organization_id": "org_123",
        "provider_id": "provider_uuid",
        "vendor_alert_id": "vendor_123",
        "alert_data": {...},
        "thumbnail_url": "https://...",
        "video_clip_url": "https://...",
        "detection_count": 1
    }
    """
    try:
        json_data = await request.json()

        # Only title + severity are universally required. camera_id/provider_id
        # are optional now so WS0 system alarms (camera offline, video-loss,
        # storage-full) — which have no analytics provider, and for storage no
        # camera — can create tickets. Analytics callers still send both.
        required_fields = ['title', 'severity']
        for field in required_fields:
            if field not in json_data:
                raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

        # Generate unique ticket number (timestamp + random suffix for uniqueness)
        import uuid
        ticket_uuid = str(uuid.uuid4())
        ticket_number = f"TKT-{int(_time.time())}-{ticket_uuid[:8]}"

        # Create ticket
        primary_event_id = json_data.get('primary_event_id')
        ticket = Ticket(
            id=ticket_uuid,
            ticket_number=ticket_number,
            title=json_data['title'],
            description=json_data.get('description'),
            severity=json_data['severity'],
            status="open",
            camera_id=json_data.get('camera_id'),
            organization_id=json_data.get('organization_id'),
            provider_id=json_data.get('provider_id'),
            vendor_alert_id=json_data.get('vendor_alert_id'),
            alarm_type=json_data.get('alarm_type'),
            primary_event_id=primary_event_id,
            is_latched=bool(json_data.get('is_latched', False)),
            alert_data=json_data.get('alert_data'),
            thumbnail_url=json_data.get('thumbnail_url'),
            video_clip_url=json_data.get('video_clip_url'),
            detection_count=json_data.get('detection_count', 0),
            created_at=_time.time(),
            updated_at=_time.time(),
            # First fire IS the latest fire at creation time.
            last_occurred_at=_time.time()
        )

        db.add(ticket)
        # Link the originating event (WS0).
        if primary_event_id is not None:
            db.add(AlarmEventLink(ticket_id=ticket_uuid, event_id=primary_event_id,
                                  created_at=_time.time()))
        await db.commit()
        await db.refresh(ticket)

        # Create initial state history
        # Use authenticated caller if available, otherwise default to system user
        user_id = caller.id if caller else 1

        state_history = TicketStateHistory(
            id=str(uuid.uuid4()),
            ticket_id=ticket.id,
            from_status="",
            to_status="open",
            changed_by_user_id=user_id,
            changed_at=_time.time()
        )
        db.add(state_history)
        await db.commit()

        logger.info("Ticket created",
                   ticket_id=ticket.id,
                   ticket_number=ticket_number,
                   severity=ticket.severity,
                   camera_id=ticket.camera_id)

        return {
            "message": "Ticket created successfully",
            "ticket_id": ticket.id,
            "ticket_number": ticket_number,
            "status": ticket.status
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to create ticket", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create ticket")


@app.post("/api/tickets/alarm")
async def upsert_alarm(
    request: Request,
    caller: Optional[User] = Depends(get_user_from_headers),
    db: AsyncSession = Depends(get_db)
):
    """Idempotent alarm intake (WS0). event-management calls this once per alarm
    event. It dedups a state alarm into a SINGLE latched ticket per
    (camera_id, alarm_type) and clears the latch when the condition ends —
    keeping every contributing event linked in alarm_event_link.

    Body:
      alarm_type   str  (required)  e.g. "camera_offline"
      event_id     int  (required)  the originating/contributing event
      is_clear     bool             true = condition cleared (e.g. camera back online)
      severity, title, description, camera_id, organization_id, alert_data
      latch        bool  default true (state alarms latch; edge alarms may not)
    """
    import uuid
    try:
        data = await request.json()
        alarm_type = data.get('alarm_type')
        event_id = data.get('event_id')
        if not alarm_type or event_id is None:
            raise HTTPException(status_code=400, detail="alarm_type and event_id are required")
        camera_id = data.get('camera_id')
        is_clear = bool(data.get('is_clear', False))
        now = _time.time()
        actor = caller.id if caller else 1

        # The active alarm ticket for this (camera, alarm_type) = one not yet
        # resolved/closed. NULL camera (e.g. storage-full) matches NULL.
        active = ("open", "assigned", "in_progress")
        q = select(Ticket).where(Ticket.alarm_type == alarm_type, Ticket.status.in_(active))
        q = q.where(Ticket.camera_id == camera_id) if camera_id is not None else q.where(Ticket.camera_id.is_(None))
        existing = (await db.execute(q.order_by(Ticket.created_at.desc()))).scalars().first()

        async def _link(tid):
            dup = (await db.execute(select(AlarmEventLink).where(
                AlarmEventLink.ticket_id == tid, AlarmEventLink.event_id == event_id))).scalars().first()
            if not dup:
                db.add(AlarmEventLink(ticket_id=tid, event_id=event_id, created_at=now))

        if is_clear:
            if not existing:
                return {"message": "no active alarm to clear", "created": False}
            await _link(existing.id)
            # Point the ticket at the LATEST occurrence, not the first.
            #
            # Only the link table was updated here, so primary_event_id stayed
            # pinned to whatever fired first. On a latched alarm that can be
            # months back: TKT-1785688251-69105db0 was created 2 August and
            # re-raised on the 11th, and its evidence clip resolved against the
            # 2 August event whose footage had long since aged out. The clip
            # must follow the occurrence the operator is looking at.
            existing.primary_event_id = event_id
            existing.latch_cleared_at = now
            existing.updated_at = now
            # If someone already responded, the alarm is fully done once the
            # condition also clears → auto-resolve.
            if existing.first_response_at:
                existing.status = "resolved"
                existing.resolved_at = now
                db.add(TicketStateHistory(id=str(uuid.uuid4()), ticket_id=existing.id,
                    from_status="", to_status="resolved", changed_by_user_id=actor,
                    reason="Auto-resolved: alarm condition cleared after acknowledgement",
                    changed_at=now))
            await db.commit()
            return {"message": "latch cleared", "ticket_id": existing.id,
                    "status": existing.status, "created": False}

        # Raising event.
        if existing:
            # Same active alarm re-fired → dedup into the one ticket.
            await _link(existing.id)
            existing.detection_count = (existing.detection_count or 0) + 1
            existing.updated_at = now
            # The alarm fired again NOW. Without this the ticket's only time
            # signal for consumers is created_at — the first-ever fire, which
            # on a latched alarm can be weeks back and whose footage is gone.
            existing.last_occurred_at = now
            existing.primary_event_id = event_id
            await db.commit()
            return {"message": "deduped into existing alarm", "ticket_id": existing.id,
                    "status": existing.status, "created": False}

        # New alarm → latched ticket.
        tid = str(uuid.uuid4())
        tnum = f"TKT-{int(now)}-{tid[:8]}"
        ticket = Ticket(
            id=tid, ticket_number=tnum,
            title=data.get('title') or alarm_type.replace("_", " ").title(),
            description=data.get('description'),
            severity=data.get('severity', 'high'),
            status="open",
            camera_id=camera_id,
            organization_id=data.get('organization_id'),
            alarm_type=alarm_type,
            primary_event_id=event_id,
            last_occurred_at=now,
            is_latched=bool(data.get('latch', True)),
            alert_data=data.get('alert_data'),
            detection_count=1,
            created_at=now, updated_at=now,
        )
        db.add(ticket)
        await _link(tid)
        db.add(TicketStateHistory(id=str(uuid.uuid4()), ticket_id=tid,
            from_status="", to_status="open", changed_by_user_id=actor, changed_at=now))
        # WS1: kick off escalation for this new alarm (match policy → fire L1 →
        # schedule). No-op if no policy matches. Same transaction.
        await _start_escalation(db, ticket)
        await db.commit()
        logger.info("Alarm ticket created", ticket_id=tid, alarm_type=alarm_type, camera_id=camera_id)
        return {"message": "alarm ticket created", "ticket_id": tid,
                "ticket_number": tnum, "status": "open", "created": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to upsert alarm", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to process alarm")


@app.post("/api/tickets/{ticket_id}/acknowledge")
async def acknowledge_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """Acknowledge a ticket (WS1) — stamps first_response/acknowledged and HALTS
    escalation (clause 50.6). Moves 'open' → 'in_progress'."""
    try:
        ticket = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        now = _time.time()
        already = ticket.acknowledged_at is not None
        old_status = ticket.status
        _stamp_ack(ticket, current_user.id, now)
        if ticket.status == "open":
            ticket.status = "in_progress"
        db.add(TicketStateHistory(id=str(_uuid.uuid4()), ticket_id=ticket.id,
            from_status=old_status, to_status=ticket.status, changed_by_user_id=current_user.id,
            reason=("Re-acknowledged" if already else "Acknowledged"), changed_at=now))
        await db.commit()
        return {"message": "acknowledged", "ticket_id": ticket.id,
                "acknowledged_at": ticket.acknowledged_at, "status": ticket.status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to acknowledge", ticket_id=ticket_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to acknowledge ticket")


@app.post("/api/tickets/{ticket_id}/assign")
async def assign_ticket(
    ticket_id: str,
    request: Request,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """Assign a ticket to a user (WS1). Taking ownership also acknowledges, so it
    halts escalation. Body: {"assigned_to_user_id": <int>}."""
    try:
        data = await request.json()
        assignee = data.get("assigned_to_user_id")
        if assignee is None:
            raise HTTPException(status_code=400, detail="assigned_to_user_id is required")
        ticket = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        now = _time.time()
        old_status = ticket.status
        ticket.assigned_to_user_id = int(assignee)
        ticket.assigned_at = now
        if ticket.status == "open":
            ticket.status = "assigned"
        _stamp_ack(ticket, current_user.id, now)
        db.add(TicketStateHistory(id=str(_uuid.uuid4()), ticket_id=ticket.id,
            from_status=old_status, to_status=ticket.status, changed_by_user_id=current_user.id,
            reason=f"Assigned to user {assignee}", changed_at=now))
        await db.commit()
        return {"message": "assigned", "ticket_id": ticket.id,
                "assigned_to_user_id": ticket.assigned_to_user_id, "status": ticket.status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to assign", ticket_id=ticket_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to assign ticket")


# ============================================================================
# WS1 — Escalation policy CRUD + test-fire (config API for the matrix builder).
# ============================================================================
def _is_admin(user) -> bool:
    return bool(user) and getattr(user, "role", None) in ("administrator", "org_admin")


def _serialize_policy(p, levels, recips_by_level, actions_by_level) -> Dict[str, Any]:
    return {
        "id": p.id, "name": p.name, "organization_id": p.organization_id, "enabled": p.enabled,
        "match_event_types": p.match_event_types, "match_severity": p.match_severity,
        "match_zone_id": p.match_zone_id, "active_window": p.active_window, "priority": p.priority,
        "levels": [{
            "id": l.id, "level_no": l.level_no, "wait_seconds": l.wait_seconds, "stop_on_ack": l.stop_on_ack,
            "recipients": [{"id": r.id, "recipient_type": r.recipient_type, "role_id": r.role_id,
                            "user_id": r.user_id, "external_ref": r.external_ref, "channels": r.channels}
                           for r in recips_by_level.get(l.id, [])],
            "actions": [{"id": a.id, "action_type": a.action_type, "params": a.params}
                        for a in actions_by_level.get(l.id, [])],
        } for l in levels],
    }


async def _write_levels(db, policy_id, levels):
    """Create level rows (+ their recipients + actions) from a payload list."""
    for lv in levels or []:
        lid = str(_uuid.uuid4())
        db.add(EscalationLevel(id=lid, policy_id=policy_id, level_no=int(lv.get("level_no", 1)),
                               wait_seconds=int(lv.get("wait_seconds", 0)),
                               stop_on_ack=bool(lv.get("stop_on_ack", True))))
        for r in lv.get("recipients", []) or []:
            db.add(EscalationRecipient(id=str(_uuid.uuid4()), level_id=lid,
                   recipient_type=r.get("recipient_type", "zone_role"), role_id=r.get("role_id"),
                   user_id=r.get("user_id"), external_ref=r.get("external_ref"),
                   channels=_clean_channels(r.get("channels"))))
        for a in lv.get("actions", []) or []:
            db.add(EscalationAction(id=str(_uuid.uuid4()), level_id=lid,
                   action_type=a.get("action_type", "notify"), params=a.get("params")))


async def _load_policy_tree(db, p) -> Dict[str, Any]:
    levels = sorted((await db.execute(
        select(EscalationLevel).where(EscalationLevel.policy_id == p.id))).scalars().all(),
        key=lambda l: l.level_no)
    lids = [l.id for l in levels]
    recips = (await db.execute(select(EscalationRecipient).where(
        EscalationRecipient.level_id.in_(lids)))).scalars().all() if lids else []
    actions = (await db.execute(select(EscalationAction).where(
        EscalationAction.level_id.in_(lids)))).scalars().all() if lids else []
    rbl, abl = {}, {}
    for r in recips:
        rbl.setdefault(r.level_id, []).append(r)
    for a in actions:
        abl.setdefault(a.level_id, []).append(a)
    return _serialize_policy(p, levels, rbl, abl)


@app.get("/api/org-roles")
async def list_org_roles(
    organization_id: Optional[str] = None,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """List the configurable role hierarchy (for the escalation matrix builder)."""
    q = select(OrgRole)
    if organization_id:
        q = q.where(or_(OrgRole.organization_id == organization_id, OrgRole.organization_id.is_(None)))
    roles = (await db.execute(q.order_by(OrgRole.rank))).scalars().all()
    return {"roles": [{"id": r.id, "name": r.name, "display_name": r.display_name, "rank": r.rank,
                       "is_system": r.is_system, "organization_id": r.organization_id} for r in roles]}


# ===== WS6: SLA policies (severity → ack/resolve deadlines) =====

def _sla_out(p):
    return {"id": p.id, "organization_id": p.organization_id, "severity": p.severity,
            "ack_seconds": p.ack_seconds, "resolve_seconds": p.resolve_seconds,
            "created_at": p.created_at, "updated_at": p.updated_at}


@app.get("/api/sla-policies")
async def list_sla_policies(current_user: User = Depends(get_current_user_flexible), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SLAPolicy).order_by(SLAPolicy.severity))).scalars().all()
    return {"sla_policies": [_sla_out(p) for p in rows]}


@app.post("/api/sla-policies")
async def upsert_sla_policy(request: Request, current_user: User = Depends(get_current_user_flexible), db: AsyncSession = Depends(get_db)):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    data = await request.json()
    sev = (data.get("severity") or "").lower().strip()
    if sev not in ("low", "medium", "high", "critical"):
        raise HTTPException(status_code=400, detail="severity must be low|medium|high|critical")
    org = data.get("organization_id")
    now = _time.time()
    existing = (await db.execute(select(SLAPolicy).where(
        SLAPolicy.severity == sev,
        SLAPolicy.organization_id == org if org else SLAPolicy.organization_id.is_(None),
    ))).scalars().first()
    if existing:
        existing.ack_seconds = data.get("ack_seconds")
        existing.resolve_seconds = data.get("resolve_seconds")
        existing.updated_at = now
        p = existing
    else:
        p = SLAPolicy(id=str(_uuid.uuid4()), organization_id=org, severity=sev,
                      ack_seconds=data.get("ack_seconds"), resolve_seconds=data.get("resolve_seconds"),
                      created_at=now, updated_at=now)
        db.add(p)
    await db.commit()
    await db.refresh(p)
    return _sla_out(p)


@app.delete("/api/sla-policies/{sla_id}")
async def delete_sla_policy(sla_id: str, current_user: User = Depends(get_current_user_flexible), db: AsyncSession = Depends(get_db)):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    await db.execute(delete(SLAPolicy).where(SLAPolicy.id == sla_id))
    await db.commit()
    return {"message": "deleted", "id": sla_id}


# ===== WS6: shift roster (zone + role + time-block → on-call user) =====

def _roster_out(r):
    return {"id": r.id, "organization_id": r.organization_id, "zone_id": r.zone_id,
            "role_name": r.role_name, "weekdays": r.weekdays, "start_time": r.start_time,
            "end_time": r.end_time, "tz": r.tz, "on_call_user_id": r.on_call_user_id,
            "created_at": r.created_at, "updated_at": r.updated_at}


@app.get("/api/shift-roster")
async def list_shift_roster(current_user: User = Depends(get_current_user_flexible), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ShiftRoster).order_by(ShiftRoster.role_name, ShiftRoster.start_time))).scalars().all()
    return {"shifts": [_roster_out(r) for r in rows]}


@app.post("/api/shift-roster")
async def create_shift(request: Request, current_user: User = Depends(get_current_user_flexible), db: AsyncSession = Depends(get_db)):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    data = await request.json()
    if not data.get("role_name") or not data.get("start_time") or not data.get("end_time") or not data.get("on_call_user_id"):
        raise HTTPException(status_code=400, detail="role_name, start_time, end_time and on_call_user_id are required")
    now = _time.time()
    r = ShiftRoster(
        id=str(_uuid.uuid4()), organization_id=data.get("organization_id"),
        zone_id=data.get("zone_id"), role_name=data["role_name"],
        weekdays=data.get("weekdays"), start_time=data["start_time"], end_time=data["end_time"],
        tz=data.get("tz") or "UTC", on_call_user_id=int(data["on_call_user_id"]),
        created_at=now, updated_at=now,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return _roster_out(r)


@app.put("/api/shift-roster/{shift_id}")
async def update_shift(shift_id: str, request: Request, current_user: User = Depends(get_current_user_flexible), db: AsyncSession = Depends(get_db)):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    data = await request.json()
    r = (await db.execute(select(ShiftRoster).where(ShiftRoster.id == shift_id))).scalars().first()
    if not r:
        raise HTTPException(status_code=404, detail="shift not found")
    for f in ("zone_id", "role_name", "weekdays", "start_time", "end_time", "tz"):
        if f in data:
            setattr(r, f, data[f])
    if "on_call_user_id" in data and data["on_call_user_id"]:
        r.on_call_user_id = int(data["on_call_user_id"])
    r.updated_at = _time.time()
    await db.commit()
    await db.refresh(r)
    return _roster_out(r)


@app.delete("/api/shift-roster/{shift_id}")
async def delete_shift(shift_id: str, current_user: User = Depends(get_current_user_flexible), db: AsyncSession = Depends(get_db)):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    await db.execute(delete(ShiftRoster).where(ShiftRoster.id == shift_id))
    await db.commit()
    return {"message": "deleted", "id": shift_id}


@app.post("/api/escalation-policies/preview")
async def preview_escalation_match(
    request: Request,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """Which policy would escalate an alarm of this shape, and who it reaches.

    Answers the question a rule author actually has — "if this fires, does
    anyone get told?" — before the rule is saved, rather than after the first
    real incident goes unanswered. Deliberately runs the SAME _match_policy the
    escalation engine runs: a preview that reimplements the matching would
    eventually disagree with it, which is worse than no preview.

    Body: {severity, camera_id?, alarm_type?, organization_id?}
    """
    data = await request.json()

    # A detached stand-in, not a persisted row: this must not create anything.
    # _match_policy only reads these four attributes.
    probe = SimpleNamespace(
        organization_id=data.get("organization_id"),
        camera_id=data.get("camera_id"),
        alarm_type=data.get("alarm_type"),
        severity=data.get("severity"),
    )

    total = (await db.execute(select(func.count()).select_from(EscalationPolicy))).scalar() or 0
    enabled = (await db.execute(select(func.count()).select_from(EscalationPolicy)
                                .where(EscalationPolicy.enabled == True))).scalar() or 0

    p = await _match_policy(db, probe)
    if not p:
        # Say WHY nothing matched — "no policy" and "policies exist but none
        # covers this" need completely different fixes from the operator.
        if total == 0:
            reason = "no_policies"
        elif enabled == 0:
            reason = "all_disabled"
        else:
            reason = "no_match"
        return {"matched": False, "reason": reason,
                "policy_count": total, "enabled_count": enabled}

    return {"matched": True, "reason": "matched", "policy": await _load_policy_tree(db, p),
            "policy_count": total, "enabled_count": enabled}


@app.get("/api/escalation-policies")
async def list_escalation_policies(
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    policies = (await db.execute(
        select(EscalationPolicy).order_by(EscalationPolicy.priority.desc()))).scalars().all()
    out = []
    for p in policies:
        n = (await db.execute(select(func.count()).select_from(EscalationLevel).where(
            EscalationLevel.policy_id == p.id))).scalar()
        out.append({"id": p.id, "name": p.name, "organization_id": p.organization_id, "enabled": p.enabled,
                    "match_event_types": p.match_event_types, "match_severity": p.match_severity,
                    "match_zone_id": p.match_zone_id, "priority": p.priority, "level_count": n})
    return {"policies": out}


@app.get("/api/escalation-policies/{policy_id}")
async def get_escalation_policy(
    policy_id: str,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    p = (await db.execute(select(EscalationPolicy).where(EscalationPolicy.id == policy_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    return await _load_policy_tree(db, p)


@app.post("/api/escalation-policies")
async def create_escalation_policy(
    request: Request,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        data = await request.json()
        if not data.get("name"):
            raise HTTPException(status_code=400, detail="name is required")
        now = _time.time()
        pid = str(_uuid.uuid4())
        db.add(EscalationPolicy(
            id=pid, name=data["name"], organization_id=data.get("organization_id"),
            enabled=bool(data.get("enabled", True)), match_event_types=data.get("match_event_types"),
            match_severity=data.get("match_severity"), match_zone_id=data.get("match_zone_id"),
            active_window=data.get("active_window"), priority=int(data.get("priority", 0)),
            created_at=now, updated_at=now))
        await _write_levels(db, pid, data.get("levels", []))
        await db.commit()
        p = (await db.execute(select(EscalationPolicy).where(EscalationPolicy.id == pid))).scalar_one()
        return await _load_policy_tree(db, p)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to create policy", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create policy")


@app.put("/api/escalation-policies/{policy_id}")
async def update_escalation_policy(
    policy_id: str,
    request: Request,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        p = (await db.execute(select(EscalationPolicy).where(EscalationPolicy.id == policy_id))).scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="Policy not found")
        data = await request.json()
        for f in ("name", "organization_id", "enabled", "match_event_types", "match_severity",
                  "match_zone_id", "active_window", "priority"):
            if f in data:
                setattr(p, f, data[f])
        p.updated_at = _time.time()
        # Replace-all for the level tree when 'levels' is provided.
        if "levels" in data:
            lids = [l.id for l in (await db.execute(
                select(EscalationLevel).where(EscalationLevel.policy_id == policy_id))).scalars().all()]
            if lids:
                await db.execute(delete(EscalationRecipient).where(EscalationRecipient.level_id.in_(lids)))
                await db.execute(delete(EscalationAction).where(EscalationAction.level_id.in_(lids)))
                await db.execute(delete(EscalationLevel).where(EscalationLevel.policy_id == policy_id))
            await _write_levels(db, policy_id, data["levels"])
        await db.commit()
        p = (await db.execute(select(EscalationPolicy).where(EscalationPolicy.id == policy_id))).scalar_one()
        return await _load_policy_tree(db, p)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update policy", policy_id=policy_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to update policy")


@app.delete("/api/escalation-policies/{policy_id}")
async def delete_escalation_policy(
    policy_id: str,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        lids = [l.id for l in (await db.execute(
            select(EscalationLevel).where(EscalationLevel.policy_id == policy_id))).scalars().all()]
        if lids:
            await db.execute(delete(EscalationRecipient).where(EscalationRecipient.level_id.in_(lids)))
            await db.execute(delete(EscalationAction).where(EscalationAction.level_id.in_(lids)))
            await db.execute(delete(EscalationLevel).where(EscalationLevel.policy_id == policy_id))
        # Detach any tickets still pointing at this policy (FK + halt their escalation).
        await db.execute(update(Ticket).where(Ticket.escalation_policy_id == policy_id).values(
            escalation_policy_id=None, next_escalation_at=None))
        await db.execute(delete(EscalationPolicy).where(EscalationPolicy.id == policy_id))
        await db.commit()
        return {"message": "deleted", "id": policy_id}
    except Exception as e:
        logger.error("Failed to delete policy", policy_id=policy_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to delete policy")


@app.post("/api/escalation-policies/{policy_id}/test-fire")
async def test_fire_policy(
    policy_id: str,
    request: Request,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """Dry-run: for a hypothetical alarm, show who would be notified at each level
    and when (cumulative seconds). Body (all optional): camera_id, alarm_type,
    severity, organization_id. Resolves real recipients via the engine — nothing
    is created or sent."""
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    p = (await db.execute(select(EscalationPolicy).where(EscalationPolicy.id == policy_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    data = await request.json() if await request.body() else {}

    # A lightweight ticket-shaped object for recipient/zone resolution.
    class _Probe:
        pass
    probe = _Probe()
    probe.camera_id = data.get("camera_id")
    probe.alarm_type = data.get("alarm_type")
    probe.severity = data.get("severity", "high")
    probe.organization_id = data.get("organization_id") or p.organization_id

    levels = sorted((await db.execute(
        select(EscalationLevel).where(EscalationLevel.policy_id == policy_id))).scalars().all(),
        key=lambda l: l.level_no)
    cum, out_levels = 0, []
    for i, l in enumerate(levels):
        if i > 0:
            cum += (l.wait_seconds or 0)
        recips = (await db.execute(select(EscalationRecipient).where(
            EscalationRecipient.level_id == l.id))).scalars().all()
        who = []
        for r in recips:
            if r.recipient_type == "zone_role" and r.role_id:
                users = await _recipients_for_role(db, probe, r.role_id)
                who += [{"user_id": u.id, "username": u.username, "role": u.role, "channels": r.channels}
                        for u in users]
            elif r.recipient_type == "user" and r.user_id:
                u = (await db.execute(select(User).where(User.id == r.user_id))).scalar_one_or_none()
                if u:
                    who.append({"user_id": u.id, "username": u.username, "role": u.role, "channels": r.channels})
            elif r.recipient_type == "external" and r.external_ref:
                who.append({"external": r.external_ref, "channels": r.channels})
        out_levels.append({"level_no": l.level_no, "fires_after_seconds": cum, "recipients": who})
    return {"policy_id": policy_id, "policy_name": p.name, "levels": out_levels}


def _wildcard_ilike_pattern(term: Optional[str]) -> Optional[str]:
    """Translate a user search term into a Postgres ILIKE pattern (clause 44.0
    wildcards): `*`→`%` (any run), `?`→`_` (any char). Existing %/_/\\ in the
    term are escaped so they stay literal; a term with no wildcard is treated as
    a substring (%term%). Returns None for empty. Use with .ilike(pat, escape='\\\\')."""
    if not term or not term.strip():
        return None
    t = term.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    if '*' in term or '?' in term:
        return t.replace('*', '%').replace('?', '_')
    return f'%{t}%'


@app.get("/api/tickets")
async def list_tickets(
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = Query(None, description="Filter by status"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    camera_id: Optional[int] = Query(None, description="Filter by camera"),
    organization_id: Optional[str] = Query(None, description="Filter by organization"),
    assigned_to: Optional[int] = Query(None, description="Filter by assigned user"),
    alarm_type: Optional[str] = Query(None, description="Filter by alarm type"),
    alarms_only: Optional[bool] = Query(None, description="Only alarm tickets (alarm_type set)"),
    search: Optional[str] = Query(None, description="Wildcard search (*, ?) over ticket #, title, description, alarm type"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    sort_by: Optional[str] = Query(None, description="created_at | updated_at | severity | status | camera | title | ticket_number"),
    sort_order: Optional[str] = Query(None, description="asc | desc (default desc)"),
    pin_latched: bool = Query(True, description="Clause 50.2: order still-asserted, unacknowledged latched alarms first, before the chosen sort. Pass false for a pure sort.")
):
    """
    List tickets with optional filters.

    Returns paginated list of tickets.
    """
    try:
        # Build query with filters
        query = select(Ticket).options(
            selectinload(Ticket.camera),
            selectinload(Ticket.provider),
            selectinload(Ticket.assigned_to)
        )

        # Apply filters
        filters = []
        if status:
            filters.append(Ticket.status == status)
        if severity:
            filters.append(Ticket.severity == severity)
        if camera_id:
            filters.append(Ticket.camera_id == camera_id)
        if organization_id:
            filters.append(Ticket.organization_id == organization_id)
        if assigned_to:
            filters.append(Ticket.assigned_to_user_id == assigned_to)
        # WS3 alarm surface: filter to alarm tickets (or a specific alarm type).
        if alarm_type:
            filters.append(Ticket.alarm_type == alarm_type)
        elif alarms_only:
            filters.append(Ticket.alarm_type.is_not(None))
        # Advanced/wildcard search (clause 44.0) over the ticket's text metadata.
        _pat = _wildcard_ilike_pattern(search)
        if _pat:
            filters.append(or_(
                Ticket.ticket_number.ilike(_pat, escape='\\'),
                Ticket.title.ilike(_pat, escape='\\'),
                Ticket.description.ilike(_pat, escape='\\'),
                Ticket.alarm_type.ilike(_pat, escape='\\'),
            ))

        if filters:
            query = query.where(and_(*filters))

        # Sortable columns (allowlisted — sort_by/sort_order were accepted but
        # silently ignored before). Severity sorts by RANK via a CASE over the
        # lowercased value: alphabetical order would put CRITICAL < HIGH < LOW
        # < MEDIUM, and stored casing is mixed across producers.
        _severity_rank = case(
            (func.lower(Ticket.severity) == 'critical', 0),
            (func.lower(Ticket.severity) == 'high', 1),
            (func.lower(Ticket.severity) == 'medium', 2),
            (func.lower(Ticket.severity) == 'low', 3),
            else_=4,
        )
        _sort_cols = {
            'created_at': Ticket.created_at,
            # Last activity. A LATCHED alarm keeps one ticket and re-raises
            # onto it, so created_at is when the alarm was first ever seen —
            # not when it last fired. Without this, an intrusion firing right
            # now sits wherever it was created, which for a long-lived latched
            # ticket is far down the list, below alarms quiet for days.
            # Observed 2026-08-11: ticket 140eb5d4 (created 07-31) updated by a
            # live intrusion and absent from the newest-12 by created_at.
            # The client's own TicketFilters type already listed 'updated_at';
            # only this allowlist was missing, so the parameter was accepted
            # and silently downgraded to created_at.
            'updated_at': Ticket.updated_at,
            'severity': _severity_rank,
            'status': func.lower(Ticket.status),
            'camera': Ticket.camera_id,   # no camera_name column; name is enriched at response time
            'title': func.lower(Ticket.title),
            'ticket_number': Ticket.ticket_number,
        }
        # Unknown values still fall back rather than 400, so a stale saved view
        # keeps working — but the fallback is no longer silent. Being quietly
        # ignored is how 'updated_at' looked supported for so long.
        if sort_by and sort_by not in _sort_cols:
            logger.warning("Unsupported sort_by ignored; falling back to created_at",
                           sort_by=sort_by, supported=sorted(_sort_cols))
        _col = _sort_cols.get(sort_by or 'created_at', Ticket.created_at)
        _primary = _col.asc() if (sort_order or 'desc').lower() == 'asc' else _col.desc()

        # Clause 50.2 — a latched alarm that is still asserted and
        # unacknowledged comes first, whatever the chosen sort.
        #
        # The web client already floats these to the top, but it can only
        # reorder the page the server sent, and the server pages by the sort
        # column. A latched ticket is created once and re-raised onto forever,
        # so its created_at is when the alarm was FIRST seen: an intrusion
        # firing right now can sit on page 4 and never reach the client's
        # reordering at all. Observed 2026-08-11 — ticket 140eb5d4, created
        # 07-31, updated by a live intrusion, absent from the newest 12.
        #
        # A parameter rather than baked-in behaviour: it defaults to the
        # compliance requirement, and a caller that wants an unmodified sort
        # (exports, reports) passes pin_latched=false. The operator's own sort
        # choice is preserved as the secondary key either way — this changes
        # what reaches page 1, not how the rest is ordered.
        _order = []
        if pin_latched:
            _active_latched = and_(
                Ticket.is_latched.is_(True),
                Ticket.acknowledged_at.is_(None),
                func.lower(Ticket.status).in_(("open", "assigned", "in_progress")),
            )
            _order.append(case((_active_latched, 0), else_=1).asc())
            # Within the pinned group, most-recently-asserted first — NOT the
            # chosen sort. 50.2 is about seeing what is wrong *now*, and a
            # latched ticket's created_at is when the alarm was first ever
            # seen. Ranking the pinned group by created_at (the first version
            # of this fix) simply reproduced the bug one level in: with 20+
            # latched alarms, one firing this minute but created weeks ago
            # still fell off page 1. Verified: ticket 140eb5d4 stayed off it.
            # Non-pinned rows get 0 here and fall through to the operator's own
            # sort untouched. Deliberately 0 rather than NULL: NULL ordering
            # under DESC is dialect-dependent (Postgres puts NULLs first, which
            # would rank a latched row with no updated_at above live ones), and
            # `.nullslast()` vs `.nulls_last()` moved between SQLAlchemy
            # versions — a runtime AttributeError here would break the entire
            # ticket list. coalesce keeps a never-updated latched row ranked by
            # its creation instead of vanishing to the bottom.
            _order.append(
                case((_active_latched, func.coalesce(Ticket.updated_at, Ticket.created_at)),
                     else_=0.0).desc()
            )
        _order.append(_primary)
        # created_at as a stable tiebreaker so equal-key pages don't shuffle.
        _order.append(Ticket.created_at.desc())
        query = query.order_by(*_order)

        # Get total count
        count_query = select(func.count()).select_from(Ticket)
        if filters:
            count_query = count_query.where(and_(*filters))

        # Zone/Area scoping (WS-Z): restrict to tickets whose camera is in the
        # caller's zones. Applied to BOTH query and count so totals stay consistent.
        zone_ids = await resolve_user_zone_ids(current_user, db)
        query = scope_by_camera_id(query, Ticket, zone_ids)
        count_query = scope_by_camera_id(count_query, Ticket, zone_ids)

        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Apply pagination
        query = query.limit(limit).offset(offset)

        # Execute query
        result = await db.execute(query)
        tickets = result.scalars().all()

        return {
            "tickets": [
                {
                    "id": t.id,
                    "ticket_number": t.ticket_number,
                    "title": t.title,
                    "description": t.description,
                    "severity": t.severity,
                    "status": t.status,
                    "camera_id": t.camera_id,
                    "camera_name": t.camera.name if t.camera else None,
                    "provider_id": t.provider_id,
                    "provider_name": t.provider.name if t.provider else None,
                    "assigned_to": t.assigned_to.username if t.assigned_to else None,
                    "created_at": t.created_at if t.created_at else None,
                    "updated_at": t.updated_at if t.updated_at else None,
                    "thumbnail_url": t.thumbnail_url,
                    "sla_breach": t.sla_breach,
                    # WS3 alarm surface: latched-alarm rendering + type filter.
                    "alarm_type": t.alarm_type,
                    "is_latched": t.is_latched,
                    "acknowledged_at": t.acknowledged_at,
                    # The occurrence this ticket currently points at. Named
                    # event_id because that is what every consumer already asks
                    # for — the column is primary_event_id, and the list simply
                    # never emitted it, so the evidence clip had nothing to
                    # resolve against and every ticket looked eventless.
                    "event_id": t.primary_event_id,
                    # When the alarm LAST fired. Distinct from created_at (the
                    # first ever fire) and updated_at (any edit, incl. comments
                    # and status changes). The evidence clip must key off this.
                    "last_occurred_at": t.last_occurred_at or t.created_at,
                }
                for t in tickets
            ],
            "total": total,
            "limit": limit,
            "offset": offset
        }

    except Exception as e:
        logger.error("Failed to list tickets", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to list tickets")


@app.get("/api/tickets/stats")
async def get_ticket_stats(
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
    organization_id: Optional[str] = Query(None)
):
    """Get ticket statistics"""
    try:
        filters = []
        if organization_id:
            filters.append(Ticket.organization_id == organization_id)

        # Zone/Area scoping (WS-Z): every count below is restricted to the
        # caller's zones so a scoped operator sees only their zones' totals.
        zone_ids = await resolve_user_zone_ids(current_user, db)
        def _scope(q):
            return scope_by_camera_id(q, Ticket, zone_ids)

        # Total tickets
        total_query = select(func.count()).select_from(Ticket)
        if filters:
            total_query = total_query.where(and_(*filters))
        total_result = await db.execute(_scope(total_query))
        total = total_result.scalar()

        # Count by status
        status_counts = {}
        for status in ['open', 'assigned', 'in_progress', 'resolved', 'closed', 'false_positive']:
            query = select(func.count()).select_from(Ticket).where(Ticket.status == status)
            if filters:
                query = query.where(and_(*filters))
            result = await db.execute(_scope(query))
            status_counts[status] = result.scalar()

        # Count by severity — case-insensitively. Stored severities are
        # mixed-case across producers ('MEDIUM' from the rule engine, 'high'
        # from event-management) and the standing decision is to NORMALIZE AT
        # READ, never rewrite stored values (WS2 preference matching keys on
        # them). Exact-match counting silently reported medium: 0 while 63
        # MEDIUM tickets sat in the list.
        severity_counts = {}
        for severity in ['critical', 'high', 'medium', 'low', 'info']:
            query = select(func.count()).select_from(Ticket).where(func.lower(Ticket.severity) == severity)
            if filters:
                query = query.where(and_(*filters))
            result = await db.execute(_scope(query))
            severity_counts[severity] = result.scalar()

        # SLA breaches
        sla_breach_query = select(func.count()).select_from(Ticket).where(Ticket.sla_breach == True)
        if filters:
            sla_breach_query = sla_breach_query.where(and_(*filters))
        sla_breach_result = await db.execute(_scope(sla_breach_query))
        sla_breaches = sla_breach_result.scalar()

        return {
            "total_tickets": total,
            "by_status": status_counts,
            "by_severity": severity_counts,
            "sla_breaches": sla_breaches,
            "timestamp": _time.time()
        }

    except Exception as e:
        logger.error("Failed to get ticket stats", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get ticket stats")


async def _build_report(ticket_id: str, current_user, db):
    """Assemble the incident report payload.

    Shared by the JSON endpoint and the PDF renderer so the printable evidence
    document and the on-screen one can never drift apart — two assemblies of
    "the same" report is exactly how a field silently stops matching."""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id).options(
            selectinload(Ticket.camera),
            selectinload(Ticket.assigned_to),
            selectinload(Ticket.comments),
            selectinload(Ticket.state_history),
        )
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Zone/Area scoping (WS-Z): same visibility rule as ticket detail.
    zone_ids = await resolve_user_zone_ids(current_user, db)
    if zone_ids is not None:
        cam_zone = ticket.camera.zone_id if ticket.camera else None
        if cam_zone not in zone_ids:
            raise HTTPException(status_code=404, detail="Ticket not found")

    notifs = (await db.execute(select(NotificationLog).where(
        NotificationLog.ticket_id == ticket_id))).scalars().all()
    comments = list(ticket.comments or [])
    history = list(ticket.state_history or [])

    uids = {h.changed_by_user_id for h in history} | {c.user_id for c in comments} | {n.user_id for n in notifs}
    if ticket.assigned_to_user_id:
        uids.add(ticket.assigned_to_user_id)
    if ticket.acknowledged_by:
        uids.add(ticket.acknowledged_by)
    uids.discard(None)
    names = {}
    if uids:
        for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all():
            names[u.id] = u.username
    who = lambda uid: names.get(uid) or (f"system" if uid in (0, 1) else (f"user {uid}" if uid else "—"))

    timeline = []
    for h in history:
        is_escalation = (h.reason or "").startswith("Escalation ")
        timeline.append({
            "ts": h.changed_at, "kind": "escalation" if is_escalation else "status",
            "actor": who(h.changed_by_user_id),
            "detail": h.reason or (f"{h.from_status} → {h.to_status}" if h.from_status else f"→ {h.to_status}"),
        })
    for c in comments:
        timeline.append({"ts": c.created_at, "kind": "comment", "actor": who(c.user_id), "detail": c.comment_text})
    for n in notifs:
        detail = f"{n.channel_type} → {n.status}"
        if n.error_message:
            detail += f" ({n.error_message})"
        timeline.append({"ts": (n.sent_at or n.failed_at or n.created_at), "kind": "notification",
                         "actor": who(n.user_id), "detail": detail})
    timeline.sort(key=lambda e: e["ts"] or 0)

    return {
        "generated_at": _time.time(),
        "generated_by": current_user.username,
        "ticket": {
            "id": ticket.id, "ticket_number": ticket.ticket_number, "title": ticket.title,
            "description": ticket.description, "severity": ticket.severity, "status": ticket.status,
            "alarm_type": ticket.alarm_type, "is_latched": ticket.is_latched,
            "last_occurred_at": ticket.last_occurred_at or ticket.created_at,
            "created_at": ticket.created_at, "resolved_at": ticket.resolved_at,
            "assigned_to": who(ticket.assigned_to_user_id) if ticket.assigned_to_user_id else None,
            "acknowledged_at": ticket.acknowledged_at, "acknowledged_by": who(ticket.acknowledged_by) if ticket.acknowledged_by else None,
            "sla_breach": ticket.sla_breach, "detection_count": ticket.detection_count,
            "thumbnail_url": ticket.thumbnail_url, "video_clip_url": ticket.video_clip_url,
        },
        "camera": {"id": ticket.camera_id, "name": ticket.camera.name if ticket.camera else None,
                   "location": getattr(ticket.camera, "location", None) if ticket.camera else None},
        "escalation": {
            "policy_id": ticket.escalation_policy_id, "level": ticket.escalation_level,
            "escalated_at": ticket.escalated_at, "next_escalation_at": ticket.next_escalation_at,
        },
        "comments": [{"actor": who(c.user_id), "text": c.comment_text, "at": c.created_at,
                      "internal": bool(c.is_internal)} for c in sorted(comments, key=lambda c: c.created_at or 0)],
        "notifications": [{"actor": who(n.user_id), "channel": n.channel_type, "status": n.status,
                           "at": (n.sent_at or n.failed_at or n.created_at), "error": n.error_message}
                          for n in sorted(notifs, key=lambda n: (n.sent_at or n.created_at or 0))],
        "timeline": timeline,
    }


@app.get("/api/tickets/{ticket_id}/report")
async def get_ticket_report(
    ticket_id: str,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    """WS5 (clauses 50.9 / 47 / 51): consolidated incident e-report + audit
    timeline for one ticket — metadata + snapshot/clip refs + comments +
    escalation fires + notification log, merged into one time-ordered audit that
    answers who-was-notified / who-ack'd / who-actioned / when. The web-client
    renders this print-ready (PDF via print) and CSV-exports the timeline."""
    return await _build_report(ticket_id, current_user, db)


# Where the evidence still comes from. recording-service resolves the wall-clock
# instant to a recording session and decodes the frame; ticket-service only
# knows WHEN, so the arithmetic deliberately stays on that side.
RECORDING_SERVICE_URL = os.getenv("SERVICE_RECORDING_SERVICE_URL", "http://recording-service:8000")
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "UTC")


async def _evidence_frame(ticket_id: str, camera_id, when_unix):
    """(jpeg_bytes, note). Never raises — a missing frame must not cost the
    report, but the reason IS carried through so the document can say why the
    picture is absent instead of showing an empty box."""
    if not camera_id:
        return None, "This incident is not associated with a camera, so no evidence frame exists."
    if not when_unix:
        return None, "The incident carries no occurrence time, so no frame could be located."
    import httpx
    url = f"{RECORDING_SERVICE_URL}/playback/{camera_id}/frame-at"
    headers = {"X-User-ID": "0", "X-User-Role": "administrator", "X-User-Name": "incident-report"}
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.get(url, params={"t": float(when_unix)}, headers=headers)
        if r.status_code == 200 and r.content:
            return r.content, ""
        detail = r.text[:180] if r.text else ""
        logger.info("Evidence frame unavailable", ticket_id=ticket_id,
                    camera_id=camera_id, status=r.status_code)
        return None, ("No recorded footage covers this moment, so no evidence frame could be "
                      f"extracted (recording service returned {r.status_code}"
                      f"{': ' + detail if detail else ''}).")
    except Exception as e:
        logger.warning("Evidence frame fetch failed", ticket_id=ticket_id, error=str(e))
        return None, (f"The evidence frame could not be retrieved ({type(e).__name__}). "
                      "The recording may still exist — retry from the ticket.")


@app.get("/api/tickets/{ticket_id}/report.pdf")
async def get_ticket_report_pdf(
    ticket_id: str,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    """The printable evidence record, rendered server-side (47.0).

    The HTML report needs a browser to become a PDF, which makes it unusable for
    anything automated — an escalation email cannot open one. This renders the
    same payload directly, so the document attached to a notification and the
    one an operator prints are the same record."""
    report = await _build_report(ticket_id, current_user, db)
    t = report.get("ticket") or {}
    cam = report.get("camera") or {}
    jpeg, note = await _evidence_frame(
        ticket_id, cam.get("id"), t.get("last_occurred_at") or t.get("created_at"))
    try:
        from incident_pdf import build_incident_pdf
        pdf = build_incident_pdf(report, jpeg, note, REPORT_TIMEZONE)
    except Exception as e:
        logger.error("Incident PDF render failed", ticket_id=ticket_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to render incident report")
    name = f"incident-{t.get('ticket_number') or ticket_id}.pdf"
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "Content-Length": str(len(pdf))},
    )


@app.get("/api/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: str,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """Get ticket details including comments and history"""
    try:
        # Get ticket with relationships
        result = await db.execute(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .options(
                selectinload(Ticket.camera),
                selectinload(Ticket.provider),
                selectinload(Ticket.assigned_to),
                selectinload(Ticket.comments),
                selectinload(Ticket.state_history)
            )
        )
        ticket = result.scalar_one_or_none()

        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        # Zone/Area scoping (WS-Z): don't reveal tickets outside the caller's zones.
        zone_ids = await resolve_user_zone_ids(current_user, db)
        if zone_ids is not None:
            cam_zone = ticket.camera.zone_id if ticket.camera else None
            if cam_zone not in zone_ids:
                raise HTTPException(status_code=404, detail="Ticket not found")

        return {
            "id": ticket.id,
            "ticket_number": ticket.ticket_number,
            "title": ticket.title,
            "description": ticket.description,
            "severity": ticket.severity,
            "status": ticket.status,
            "camera_id": ticket.camera_id,
            "camera_name": ticket.camera.name if ticket.camera else None,
            "provider_id": ticket.provider_id,
            "provider_name": ticket.provider.name if ticket.provider else None,
            "vendor_alert_id": ticket.vendor_alert_id,
            "assigned_to": ticket.assigned_to.username if ticket.assigned_to else None,
            "assigned_at": ticket.assigned_at if ticket.assigned_at else None,
            "alert_data": ticket.alert_data,
            "thumbnail_url": ticket.thumbnail_url,
            "video_clip_url": ticket.video_clip_url,
            "detection_count": ticket.detection_count,
            "sla_breach": ticket.sla_breach,
            "sla_breach_reason": ticket.sla_breach_reason,
            "first_response_time_seconds": ticket.first_response_time_seconds,
            "resolution_time_seconds": ticket.resolution_time_seconds,
            "created_at": ticket.created_at if ticket.created_at else None,
            "updated_at": ticket.updated_at if ticket.updated_at else None,
            # When the alarm LAST fired — what the evidence clip must key off.
            # created_at is the first-ever fire (weeks back on a latched alarm,
            # with its footage long deleted); updated_at also moves for comments
            # and status changes, so it is not a substitute.
            "last_occurred_at": ticket.last_occurred_at or ticket.created_at,
            "comments": [
                {
                    "id": c.id,
                    "comment": c.comment_text,
                    "is_internal": c.is_internal,
                    "created_at": c.created_at if c.created_at else None
                }
                for c in ticket.comments
            ],
            "state_history": [
                {
                    "id": h.id,
                    "from_status": h.from_status,
                    "to_status": h.to_status,
                    "changed_by_user_id": h.changed_by_user_id,
                    "changed_at": h.changed_at if h.changed_at else None
                }
                for h in ticket.state_history
            ]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get ticket", ticket_id=ticket_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get ticket")


@app.patch("/api/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: str,
    request: Request,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """
    Update ticket status.

    Body:
    {
        "status": "assigned|in_progress|resolved|closed|false_positive",
        "reason": "Optional note recorded as a comment",
        "comment": "Accepted as an alias for `reason`"
    }

    `reason` is what the web client has always sent (resolveTicket / closeTicket
    both take one); this endpoint only ever read `comment`, so an operator's
    stated reason for resolving or closing was accepted and silently dropped.
    Both spellings are read now — `reason` first, since that is the live caller.
    """
    try:
        json_data = await request.json()
        new_status = json_data.get('status')

        if not new_status:
            raise HTTPException(status_code=400, detail="Missing required field: status")

        # Validate status
        valid_statuses = ['open', 'assigned', 'in_progress', 'resolved', 'closed', 'false_positive']
        if new_status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

        # Get ticket
        result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = result.scalar_one_or_none()

        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        old_status = ticket.status
        now = _time.time()

        # Update ticket
        ticket.status = new_status
        ticket.updated_at = now

        # WS1: any move off 'open' is a response → stamp ack + halt escalation.
        if new_status != "open":
            _stamp_ack(ticket, current_user.id, now)
        # Resolution timestamps + SLA duration.
        if new_status in ("resolved", "closed", "false_positive"):
            if ticket.resolved_at is None:
                ticket.resolved_at = now
                ticket.resolution_time_seconds = int(now - (ticket.created_at or now))
            if new_status == "closed":
                ticket.closed_at = now
            ticket.next_escalation_at = None

        import uuid
        state_history = TicketStateHistory(
            id=str(uuid.uuid4()),
            ticket_id=ticket.id,
            from_status=old_status,
            to_status=new_status,
            changed_by_user_id=current_user.id,
            changed_at=_time.time()
        )
        db.add(state_history)

        # Record the operator's note, under either spelling (see _status_note).
        note = _status_note(json_data)
        if note:
            comment = TicketComment(
                id=str(uuid.uuid4()),
                ticket_id=ticket.id,
                user_id=current_user.id,
                comment_text=note,
                is_internal=json_data.get('is_internal', False),
                created_at=_time.time()
            )
            db.add(comment)

        await db.commit()

        logger.info("Ticket status updated",
                   ticket_id=ticket_id,
                   old_status=old_status,
                   new_status=new_status)

        return {
            "message": "Ticket status updated",
            "ticket_id": ticket_id,
            "old_status": old_status,
            "new_status": new_status
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update ticket status", ticket_id=ticket_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to update ticket status")


@app.post("/api/tickets/{ticket_id}/comments")
async def add_comment(
    ticket_id: str,
    request: Request,
    current_user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db)
):
    """
    Add a comment to a ticket.

    Body:
    {
        "comment": "Comment text",
        "is_internal": false
    }
    """
    try:
        json_data = await request.json()

        if not json_data.get('comment'):
            raise HTTPException(status_code=400, detail="Missing required field: comment")

        # Verify ticket exists
        result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = result.scalar_one_or_none()

        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        # Create comment
        import uuid
        comment = TicketComment(
            id=str(uuid.uuid4()),
            ticket_id=ticket_id,
            user_id=current_user.id,
            comment_text=json_data['comment'],
            is_internal=json_data.get('is_internal', False),
            created_at=_time.time()
        )

        db.add(comment)
        await db.commit()
        await db.refresh(comment)

        logger.info("Comment added to ticket",
                   ticket_id=ticket_id,
                   comment_id=comment.id)

        return {
            "message": "Comment added successfully",
            "comment_id": comment.id,
            "ticket_id": ticket_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to add comment", ticket_id=ticket_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to add comment")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=os.getenv("DEBUG", "False").lower() == "true"
    )
