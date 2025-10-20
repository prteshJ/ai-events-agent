# storage.py — Database connection and ORM models using SQLAlchemy
#
# Handles DB connection setup, ORM model definition, and session management.
# Supports PostgreSQL with connection timeout and future-proof config.
#
# Exposes:
#   - Event ORM model
#   - init_db() to create tables safely
#   - get_db() generator for dependency injection in FastAPI

import os
from datetime import datetime

from sqlalchemy import create_engine, String, Text, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column

# Read DATABASE_URL from environment variable, fallback to placeholder
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname")

# Connection arguments, add timeout for Postgres to avoid hanging on startup
CONNECT_ARGS = {}
if DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    CONNECT_ARGS["connect_timeout"] = 5  # seconds

# Create SQLAlchemy engine with pool pre-ping to keep connections alive
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args=CONNECT_ARGS,
)

# Session factory for DB transactions
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Base class for ORM models
Base = declarative_base()

class Event(Base):
    """
    ORM model for calendar events stored in the 'events' table.

    Columns:
    - id: Primary key (string, max 120 chars)
    - title: Event title (string, required)
    - start: Event start datetime as string (nullable)
    - end: Event end datetime as string (nullable)
    - location: Event location (nullable)
    - description: Event description (nullable)
    - recurring: Whether event recurs (bool, default False)
    - recurrence_rule: Rule describing recurrence (nullable)
    - source_type: Source of event info (e.g., 'email') (required)
    - source_message_id: ID of source message (required)
    - source_snippet: Snippet from source for context (nullable)
    - created_at: Timestamp when event was created
    - updated_at: Timestamp when event was last updated
    """
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

def init_db():
    """
    Initialize database by creating all tables defined in ORM models.
    Should be called on startup to ensure schema exists.
    """
    Base.metadata.create_all(bind=engine)

def get_db():
    """
    Provides a database session generator for FastAPI dependency injection.
    Ensures session is closed after use to release connections back to pool.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
