"""Construct the shared local services. No messenger objects."""

from __future__ import annotations

from dataclasses import dataclass

from config import DB_PATH, OLLAMA_API_BASE, PALACE_PATH
from core.privacy import bootstrap
from database.db import DatabaseManager
from llm.llm_handler import LLMHandler
from memory.palace import PalaceAdapter


@dataclass
class Runtime:
    db: DatabaseManager
    llm: LLMHandler
    palace: PalaceAdapter


def build_runtime(
    *,
    db_path: str = DB_PATH,
    ollama_api_base: str = OLLAMA_API_BASE,
    palace_path: str = PALACE_PATH,
) -> Runtime:
    bootstrap()
    return Runtime(
        db=DatabaseManager(db_path),
        llm=LLMHandler(ollama_api_base),
        palace=PalaceAdapter(palace_path),
    )
