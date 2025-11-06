# Ticket Service

Centralized ticket management service for the Phase 2 analytics platform.

## Overview

The Ticket Service handles:
- **Alert-to-Ticket Conversion**: Convert analytics alerts from providers into tickets
- **Ticket Lifecycle Management**: Track ticket status through open → assigned → in_progress → resolved → closed
- **SLA Tracking**: Monitor first response time and resolution time
- **Comment System**: Internal and external comments on tickets
- **State History**: Track all status changes
- **Statistics**: Aggregate ticket metrics by status, severity, provider, etc.

## Architecture

- **Framework**: FastAPI
- **Database**: PostgreSQL (shared with VMS)
- **Deployment**: Internal-only service (accessed through API Gateway)
- **Authentication**: Handled by API Gateway

## API Endpoints

### Health Check
- `GET /health` - Health check endpoint

### Ticket Management
- `POST /api/tickets` - Create a new ticket from analytics alert
- `GET /api/tickets` - List tickets with filters (status, severity, camera, etc.)
- `GET /api/tickets/{ticket_id}` - Get ticket details with comments and history
- `PATCH /api/tickets/{ticket_id}/status` - Update ticket status
- `POST /api/tickets/{ticket_id}/comments` - Add comment to ticket
- `GET /api/tickets/stats` - Get ticket statistics

## Database Schema

The service uses the following tables (created by Phase 2 migration):
- `tickets` - Main ticket records
- `ticket_comments` - Comments on tickets
- `ticket_state_history` - Status change history
- `notification_logs` - Notification delivery tracking

## Ticket Lifecycle

1. **open** - Initial state when alert is received
2. **assigned** - Ticket assigned to a user
3. **in_progress** - User is actively working on ticket
4. **resolved** - Issue resolved, awaiting verification
5. **closed** - Ticket completed and archived
6. **false_positive** - Alert was incorrect (no action needed)

## SLA Tracking

The service tracks two key SLA metrics:
- **First Response Time**: Time from ticket creation to first user action
- **Resolution Time**: Time from creation to resolution/closure

SLA breaches are flagged automatically based on severity thresholds.

## Integration

### Called By
- **Analytics Providers**: Create tickets when alerts are generated
- **API Gateway**: Proxies all user requests

### Calls To
- **Notification Service**: Send notifications for ticket events (future)

## Development

### Local Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.template .env
# Edit .env with your configuration

# Run service
python main.py
```

### Docker Build

```bash
docker build -t vms-ticket-service:standalone .
```

### Testing

```bash
# Health check
curl http://localhost:8000/health

# Create ticket (requires auth via API Gateway in production)
curl -X POST http://localhost:8000/api/tickets \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Person detected in restricted zone",
    "severity": "high",
    "camera_id": 1,
    "provider_id": "provider-uuid",
    "alert_data": {"detections": []}
  }'
```

## Configuration

### Environment Variables

- `DATABASE_URL`: PostgreSQL connection string
- `SERVICE_NAME`: Service identifier (default: ticket-service)
- `LOG_LEVEL`: Logging level (default: INFO)

## Future Enhancements

- [ ] Email notifications for ticket events
- [ ] Webhook integrations for external ticketing systems
- [ ] Bulk ticket operations
- [ ] Advanced search and filtering
- [ ] Ticket templates
- [ ] Custom fields per organization
- [ ] Ticket escalation rules
- [ ] Automated ticket assignment

## Related Services

- **Analytics Service**: Manages analytics providers
- **Notification Service**: Sends alerts to users (Phase 2 Week 2)
- **API Gateway**: Provides authentication and routing
