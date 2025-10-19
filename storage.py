# storage.py
import os
from datetime import datetime
from sqlalchemy import create_engine, String, Text, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname")

# Add a short connect timeout for psycopg
CONNECT_ARGS = {}
if DATABASE_URL.startswith(("postgresql+psycopg://", "postgresql://")):
    CONNECT_ARGS["connect_timeout"] = 5  # seconds

# Optional: enforce a short statement timeout (5s) via query param
if "statement_timeout" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}options=-c%20statement_timeout=5000"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args=CONNECT_ARGS,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()
