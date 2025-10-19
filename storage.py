# storage.py
import os
from datetime import datetime
from sqlalchemy import create_engine, String, Text, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname")

CONNECT_ARGS = {}
if DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    CONNECT_ARGS["connect_timeout"] = 5

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args=CONNECT_ARGS,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class Event(Base):
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
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
