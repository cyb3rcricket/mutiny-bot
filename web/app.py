"""Application factory. One process, one scheduler, no messenger."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from config import (
    BIND_HOST,
    DB_PATH,
    LEGACY_SCHEDULER_DB_PATH,
    OLLAMA_API_BASE,
    PALACE_PATH,
    PORT,
    SCHEDULER_DB_PATH,
)
from core.privacy import bootstrap
from database.db import DatabaseManager
from llm.llm_handler import LLMHandler
from memory.palace import PalaceAdapter
from scheduler.scheduler_manager import SchedulerManager
from tools import register_tools
from web.routes.api import router
from web.security import LocalSessionMiddleware, json_error, new_session_token

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    *,
    db_path: str = DB_PATH,
    scheduler_db_path: str = SCHEDULER_DB_PATH,
    legacy_scheduler_db_path: str = LEGACY_SCHEDULER_DB_PATH,
    ollama_api_base: str = OLLAMA_API_BASE,
    palace_path: str = PALACE_PATH,
    llm: object | None = None,
) -> FastAPI:
    bootstrap()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = DatabaseManager(db_path)
        await db.setup_database()
        register_tools()
        palace = PalaceAdapter(palace_path)
        model = llm if llm is not None else LLMHandler(ollama_api_base)
        scheduler = SchedulerManager(db, scheduler_db_path, legacy_scheduler_db_path)
        await scheduler.start_scheduler()
        app.state.services = SimpleNamespace(db=db, llm=model, palace=palace, scheduler=scheduler)
        app.state.session_token = new_session_token()
        try:
            yield
        finally:
            scheduler.shutdown()
            await db.close()

    app = FastAPI(title="Mutiny", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.bind_host = BIND_HOST
    app.state.port = PORT
    app.add_middleware(LocalSessionMiddleware)
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return json_error(422, "invalid_request", "The request could not be validated.")

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
