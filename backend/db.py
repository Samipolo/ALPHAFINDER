"""Database layer for AlphaFinder auth: users + sessions.

Uses Postgres in production (Render DATABASE_URL) and falls back to a local
SQLite file for development when no DATABASE_URL is set.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session as OrmSession, sessionmaker

from config import CACHE_DIR

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    # Render (like old Heroku) may hand out the legacy "postgres://" scheme;
    # SQLAlchemy 1.4+/2.x requires "postgresql://".
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

if not DATABASE_URL:
    _local_path = os.path.join(CACHE_DIR, "alphafinder_local.db").replace("\\", "/")
    DATABASE_URL = f"sqlite:///{_local_path}"

_engine_kwargs: dict = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(320), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class UserSession(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    user_agent = Column(String(400), default="")
    ip_address = Column(String(64), default="")
    created_at = Column(DateTime, default=_utcnow)
    last_seen_at = Column(DateTime, default=_utcnow)
    revoked = Column(Boolean, default=False, nullable=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()