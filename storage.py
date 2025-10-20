# storage.py

import os
import traceback
from datetime import datetime

from sqlalchemy import create_engine, String, Text, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column

# -----------------------------------------------------------------------------
# Config: DATABASE_URL
# -----------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname")

if DATABASE_URL == "postgresql://user:pass@host/dbname":
    print("[storage] ⚠️ WARNING: Using default placeholder DATABASE_URL — check your Railway environment!")

CONNECT_ARGS = {}
if DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    CONNECT_ARGS["connect_timeout"] = 5

# -----------------------------------------------------------------------------
# Engine / Session
# -----------------------------------------------------------------------------
try:
    print(f"[storage] Creating engine for {DATABASE_URL}")
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        future=True,
        connect_args=CONNECT_ARGS,
    )
except Exception:
    print("[storage] ❌ Failed to create engine:")
    traceback.print_exc()
    raise

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

# -----------------------------------------------------------------------------
# Event Model
# -----------------------------------------------------------------------------
class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    start: Mapped[str | None] = mapped_column(String(64), nullable=True)
