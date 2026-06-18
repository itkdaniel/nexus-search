"""
SQLAlchemy ORM model and Pydantic schemas for Projects.

Matches the main portfolio's projects table schema exactly so nexus-search
can read from the same PostgreSQL database.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import uuid as _uuid

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, String, Text, TypeDecorator
from sqlalchemy.types import JSON, TEXT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


def _new_uuid() -> str:
    return str(_uuid.uuid4())


# ── Cross-dialect array column (SQLite=JSON text, PostgreSQL=native ARRAY) ────

class StringList(TypeDecorator):
    """
    Stores List[str] as a JSON-encoded TEXT in SQLite (tests) and as a
    native PostgreSQL ARRAY in production.  Transparent to the ORM.
    """
    impl = TEXT
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import ARRAY
            return dialect.type_descriptor(ARRAY(String))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if dialect.name == "postgresql":
            return value  # asyncpg handles list natively
        import json
        return json.dumps(value or [])

    def process_result_value(self, value, dialect):
        if dialect.name == "postgresql":
            return value or []
        if value is None:
            return []
        import json
        if isinstance(value, list):
            return value
        return json.loads(value)


# ── ORM Model ─────────────────────────────────────────────────────────────────

class ProjectModel(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=_new_uuid
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    long_description: Mapped[Optional[str]] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String, nullable=False)
    tags: Mapped[List[str]] = mapped_column(StringList, default=list)
    image_url: Mapped[Optional[str]] = mapped_column(String)
    github_url: Mapped[Optional[str]] = mapped_column(String)
    run_command: Mapped[Optional[str]] = mapped_column(String)
    test_command: Mapped[Optional[str]] = mapped_column(String)
    usage_instructions: Mapped[Optional[str]] = mapped_column(Text)
    download_url: Mapped[Optional[str]] = mapped_column(String)
    sandbox_url: Mapped[Optional[str]] = mapped_column(String)
    demo_api_endpoint: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    featured: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class ProjectBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1)
    tags: List[str] = Field(default_factory=list)
    long_description: Optional[str] = None
    image_url: Optional[str] = None
    github_url: Optional[str] = None
    run_command: Optional[str] = None
    test_command: Optional[str] = None
    usage_instructions: Optional[str] = None
    download_url: Optional[str] = None
    sandbox_url: Optional[str] = None
    demo_api_endpoint: Optional[str] = None
    published: bool = True
    featured: bool = False
    status: str = "active"


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    """Partial update — all fields optional."""
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    tags: Optional[List[str]] = None
    long_description: Optional[str] = None
    image_url: Optional[str] = None
    github_url: Optional[str] = None
    run_command: Optional[str] = None
    test_command: Optional[str] = None
    usage_instructions: Optional[str] = None
    download_url: Optional[str] = None
    sandbox_url: Optional[str] = None
    demo_api_endpoint: Optional[str] = None
    published: Optional[bool] = None
    featured: Optional[bool] = None
    status: Optional[str] = None


class ProjectResponse(ProjectBase):
    id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
