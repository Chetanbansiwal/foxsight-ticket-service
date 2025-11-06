"""
Ticket Service

Centralized ticket management service for Phase 2 analytics platform.
Handles alert-to-ticket conversion, ticket lifecycle, and SLA tracking.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, update, func
from sqlalchemy.orm import selectinload
import structlog

# Import shared modules
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '../../shared'))

from database import db_manager, get_db
from models import (
    Ticket, TicketComment, TicketStateHistory,
    NotificationLog, AnalyticsProvider, User, Camera,
    TicketStatus
)

# Configure logging
logger = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    logger.info("Starting Ticket Service...")
    await db_manager.initialize()
    logger.info("Ticket Service started successfully")

    yield

    # Shutdown
    logger.info("Shutting down Ticket Service...")
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
            "timestamp": datetime.utcnow().isoformat()
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

        # Validate required fields
        required_fields = ['title', 'severity', 'camera_id', 'provider_id']
        for field in required_fields:
            if field not in json_data:
                raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

        # Generate ticket number
        ticket_number = f"TKT-{int(datetime.utcnow().timestamp())}"

        # Create ticket
        import uuid
        ticket = Ticket(
            id=str(uuid.uuid4()),
            ticket_number=ticket_number,
            title=json_data['title'],
            description=json_data.get('description'),
            severity=json_data['severity'],
            status="open",
            camera_id=json_data['camera_id'],
            organization_id=json_data.get('organization_id'),
            provider_id=json_data['provider_id'],
            vendor_alert_id=json_data.get('vendor_alert_id'),
            alert_data=json_data.get('alert_data'),
            thumbnail_url=json_data.get('thumbnail_url'),
            video_clip_url=json_data.get('video_clip_url'),
            detection_count=json_data.get('detection_count', 0),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

        db.add(ticket)
        await db.commit()
        await db.refresh(ticket)

        # Create initial state history
        state_history = TicketStateHistory(
            id=str(uuid.uuid4()),
            ticket_id=ticket.id,
            old_status=None,
            new_status="open",
            changed_at=datetime.utcnow()
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


@app.get("/api/tickets")
async def list_tickets(
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = Query(None, description="Filter by status"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    camera_id: Optional[int] = Query(None, description="Filter by camera"),
    organization_id: Optional[str] = Query(None, description="Filter by organization"),
    assigned_to: Optional[int] = Query(None, description="Filter by assigned user"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0)
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

        if filters:
            query = query.where(and_(*filters))

        # Order by created_at descending
        query = query.order_by(Ticket.created_at.desc())

        # Get total count
        count_query = select(func.count()).select_from(Ticket)
        if filters:
            count_query = count_query.where(and_(*filters))
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
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                    "thumbnail_url": t.thumbnail_url,
                    "sla_breach": t.sla_breach
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


@app.get("/api/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: str,
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
            "assigned_at": ticket.assigned_at.isoformat() if ticket.assigned_at else None,
            "alert_data": ticket.alert_data,
            "thumbnail_url": ticket.thumbnail_url,
            "video_clip_url": ticket.video_clip_url,
            "detection_count": ticket.detection_count,
            "sla_breach": ticket.sla_breach,
            "sla_breach_reason": ticket.sla_breach_reason,
            "first_response_time_seconds": ticket.first_response_time_seconds,
            "resolution_time_seconds": ticket.resolution_time_seconds,
            "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
            "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
            "comments": [
                {
                    "id": c.id,
                    "comment": c.comment,
                    "is_internal": c.is_internal,
                    "created_at": c.created_at.isoformat() if c.created_at else None
                }
                for c in ticket.comments
            ],
            "state_history": [
                {
                    "id": h.id,
                    "old_status": h.old_status,
                    "new_status": h.new_status,
                    "changed_at": h.changed_at.isoformat() if h.changed_at else None
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
    db: AsyncSession = Depends(get_db)
):
    """
    Update ticket status.

    Body:
    {
        "status": "assigned|in_progress|resolved|closed|false_positive",
        "comment": "Optional comment"
    }
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

        # Update ticket
        ticket.status = new_status
        ticket.updated_at = datetime.utcnow()

        # Create state history
        import uuid
        state_history = TicketStateHistory(
            id=str(uuid.uuid4()),
            ticket_id=ticket.id,
            old_status=old_status,
            new_status=new_status,
            changed_at=datetime.utcnow()
        )
        db.add(state_history)

        # Add comment if provided
        if json_data.get('comment'):
            comment = TicketComment(
                id=str(uuid.uuid4()),
                ticket_id=ticket.id,
                comment=json_data['comment'],
                is_internal=json_data.get('is_internal', False),
                created_at=datetime.utcnow()
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
            comment=json_data['comment'],
            is_internal=json_data.get('is_internal', False),
            created_at=datetime.utcnow()
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


@app.get("/api/tickets/stats")
async def get_ticket_stats(
    db: AsyncSession = Depends(get_db),
    organization_id: Optional[str] = Query(None)
):
    """Get ticket statistics"""
    try:
        filters = []
        if organization_id:
            filters.append(Ticket.organization_id == organization_id)

        # Total tickets
        total_query = select(func.count()).select_from(Ticket)
        if filters:
            total_query = total_query.where(and_(*filters))
        total_result = await db.execute(total_query)
        total = total_result.scalar()

        # Count by status
        status_counts = {}
        for status in ['open', 'assigned', 'in_progress', 'resolved', 'closed', 'false_positive']:
            query = select(func.count()).select_from(Ticket).where(Ticket.status == status)
            if filters:
                query = query.where(and_(*filters))
            result = await db.execute(query)
            status_counts[status] = result.scalar()

        # Count by severity
        severity_counts = {}
        for severity in ['critical', 'high', 'medium', 'low', 'info']:
            query = select(func.count()).select_from(Ticket).where(Ticket.severity == severity)
            if filters:
                query = query.where(and_(*filters))
            result = await db.execute(query)
            severity_counts[severity] = result.scalar()

        # SLA breaches
        sla_breach_query = select(func.count()).select_from(Ticket).where(Ticket.sla_breach == True)
        if filters:
            sla_breach_query = sla_breach_query.where(and_(*filters))
        sla_breach_result = await db.execute(sla_breach_query)
        sla_breaches = sla_breach_result.scalar()

        return {
            "total_tickets": total,
            "by_status": status_counts,
            "by_severity": severity_counts,
            "sla_breaches": sla_breaches,
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error("Failed to get ticket stats", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get ticket stats")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=os.getenv("DEBUG", "False").lower() == "true"
    )
