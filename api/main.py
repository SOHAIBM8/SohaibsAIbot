"""
Dashboard API entry point (docs/dashboard_ui_spec.md). A thin,
strongly-typed wrapper over `core/` — this module and everything under
`api/` must never compute a signal, size a position, or make a risk
decision; it only exposes what `core/` already decided (spec section 1).

Run locally:
    uvicorn api.main:app --reload --port 8000
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.ai_assistant_templates import ensure_templates_registered
from api.auth.router import router as auth_router
from api.config import load_settings
from api.routes.ai_assistant import router as ai_assistant_router
from api.routes.dashboard import router as dashboard_router
from api.routes.experiments import router as experiments_router
from api.routes.health import router as health_router
from api.routes.market import router as market_router
from api.routes.notifications import router as notifications_router
from api.routes.orders import router as orders_router
from api.routes.portfolio import router as portfolio_router
from api.routes.positions import router as positions_router
from api.routes.risk import router as risk_router
from api.routes.settings import router as settings_router
from api.security_headers import security_headers_middleware
from api.websocket.account_resolver import OrderAccountResolver
from api.websocket.connection_manager import ConnectionManager
from api.websocket.gateway import EventGateway
from api.websocket.router import router as websocket_router
from core.db import SessionLocal
from core.ingestion.event_bus import PostgresEventBus
from core.notifications.notification_log import NotificationLogStore
from core.notifications.notification_persister import NotificationPersister

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Idempotent (ON CONFLICT DO UPDATE) and independent of the event
    # gateway — always run, so the AI Assistant's own well-known
    # templates exist before any explanation endpoint is first called.
    db = SessionLocal()
    try:
        ensure_templates_registered(db)
    finally:
        db.close()

    app.state.connection_manager = ConnectionManager()

    event_bus = PostgresEventBus()
    gateway = EventGateway(
        event_bus, app.state.connection_manager, OrderAccountResolver(SessionLocal)
    )
    # A second, independent subscriber on the same bus — persists a
    # fixed subset of events (severity-mapped) to notification_log,
    # not another WebSocket broadcaster. See
    # core/notifications/notification_persister.py's module docstring.
    notification_persister = NotificationPersister(
        event_bus, store_factory=lambda: NotificationLogStore(SessionLocal())
    )
    app.state.event_bus = event_bus
    app.state.event_gateway = gateway
    app.state.notification_persister = notification_persister

    if settings.enable_event_gateway:
        gateway.start()  # subscribes handlers, captures the running loop
        notification_persister.start()  # subscribes handlers, no running loop needed
        event_bus.start()  # starts the LISTEN/NOTIFY background thread

    yield

    if settings.enable_event_gateway:
        event_bus.close()


app = FastAPI(title="Trading Platform Dashboard API", version="0.1.0", lifespan=lifespan)

app.middleware("http")(security_headers_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Required for cookies to be sent cross-origin in dev (SPA on
    # :5173, API on :8000).
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(ai_assistant_router)
app.include_router(dashboard_router)
app.include_router(experiments_router)
app.include_router(risk_router)
app.include_router(settings_router)
app.include_router(orders_router)
app.include_router(portfolio_router)
app.include_router(market_router)
app.include_router(positions_router)
app.include_router(notifications_router)
app.include_router(websocket_router)
