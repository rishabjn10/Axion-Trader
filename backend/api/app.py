"""
FastAPI application factory for axion-trader REST API.

Creates and configures the FastAPI application with:
- CORS middleware for React dashboard access
- Lifespan context manager for startup/shutdown hooks
- Router inclusion from routes.py
- Automatic /docs and /redoc OpenAPI documentation

The API server runs in a daemon thread started by main.py, allowing the
agent's asyncio loops to run in the main thread without interference.

Role in system: HTTP interface between the agent backend and the React dashboard.
All agent state, trade history, and metrics are read from SQLite via the
routes module. Mode changes are applied to the agent_state table.

Dependencies: fastapi, loguru, config.settings, api.routes
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from backend.config.settings import settings

# Module-level startup time for uptime calculation in /api/health
_START_TIME: float = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI lifespan context manager for startup and shutdown logic.

    Runs database initialisation on startup and logs shutdown on teardown.
    The lifespan pattern ensures cleanup happens even if the server is
    killed with SIGINT.

    Args:
        app: The FastAPI application instance (injected by FastAPI).

    Yields:
        None — control passes to the application during the yield.

    Example:
        >>> # Used internally by FastAPI — not called directly
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("FastAPI API server starting up…")
    from backend.memory.store import init_db
    init_db()
    logger.info(f"API server ready at http://{settings.api_host}:{settings.api_port}")
    logger.info(f"Docs available at http://{settings.api_host}:{settings.api_port}/docs")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("FastAPI API server shutting down")


def create_app() -> FastAPI:
    """
    Application factory — creates and configures the FastAPI app.

    Uses the factory pattern rather than a module-level app instance to
    allow for easier testing and multiple configuration profiles.

    Returns:
        Configured FastAPI application ready to serve requests.

    Example:
        >>> app = create_app()
        >>> # uvicorn.run(app, host='0.0.0.0', port=8000)
    """
    _app = FastAPI(
        title="axion-trader API",
        description=(
            "REST API for the axion-trader autonomous AI trading agent. "
            "Provides real-time access to agent state, trade history, "
            "performance metrics, and mode control."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── CORS middleware ────────────────────────────────────────────────────────
    # Allow the React dev server (and any configured origins) to call the API.
    _app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── Include API routes ────────────────────────────────────────────────────
    from backend.api.routes import router
    _app.include_router(router)

    return _app


# Module-level app instance — used by uvicorn when run as:
# uvicorn backend.api.app:app
app: FastAPI = create_app()

# Expose start time for uptime calculation
START_TIME = _START_TIME
