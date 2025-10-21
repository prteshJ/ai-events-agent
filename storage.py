# storage.py — Database connection and ORM models using SQLAlchemy
#
# Exposes:
#   - Event ORM model
#   - init_db()  : create tables if missing
#   - get_db()   : session generator for FastAPI

import os
from datetime import datetime

from sqlalchemy import create_engine, String, Text, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname")

# Keep it simple and reliable
CONNECT_ARGS = {}
if DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    CONNECT_ARGS["connect_timeout"] = 5  # seconds

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args=CONNECT_ARGS,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class Event(Base):
    """
    Event stored in 'events' table.

    Notes:
    - start/end are strings (ISO-8601). This keeps v1 simple.
      (We can migrate to TIMESTAMPTZ later without changing the API.)
    """
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)

    # v1: ISO strings (e.g., "2025-10-21T09:30:00+00:00")
    start: Mapped[str | None] = mapped_column(String(64), nullable=True)
    end: Mapped[str | None] = mapped_column(String(64), nullable=True)

    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recurrence_rule: Mapped[str | None] = mapped_column(String(300), nullable=True)

    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # small improvement: auto-update on row changes
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

def init_db():
    """Create tables if they don't exist."""
    Base.metadata.create_all(bind=engine)

def get_db():
    """Yield a DB session (FastAPI Depends)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
