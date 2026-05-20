"""
models.py
=========
Database models and connection setup.

DEFAULT: SQLite — zero configuration, works out of the box.
  Data is saved to  econ_dashboard.db  in the same folder as main.py.

OPTIONAL (PostgreSQL): set the DATABASE_URL environment variable:
  Windows:  set DATABASE_URL=postgresql://postgres:senha@localhost:5432/econ_dashboard
  Linux:    export DATABASE_URL=postgresql://postgres:senha@localhost:5432/econ_dashboard
  .env file: DATABASE_URL=postgresql://postgres:senha@localhost:5432/econ_dashboard
"""

import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    Date, DateTime, Text, UniqueConstraint, Index, event
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# ── Detect which database to use ──────────────────────────────────────────────
# Priority: environment variable → SQLite (default, no setup needed)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///./econ_dashboard.db"   # ← works with zero configuration
)

IS_SQLITE = DATABASE_URL.startswith("sqlite")

# ── Engine ───────────────────────────────────────────────────────────────────
# SQLite needs connect_args to allow multi-thread access (required by FastAPI)
if IS_SQLITE:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ── Models ───────────────────────────────────────────────────────────────────

class IndexHistory(Base):
    __tablename__ = "indices_history"

    id         = Column(Integer, primary_key=True)
    date       = Column(Date, nullable=False)
    index_name = Column(String(10), nullable=False)
    value      = Column(Float, nullable=False)
    fetched_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "index_name", name="uq_history_date_index"),
        Index("ix_history_date_index", "date", "index_name"),
    )

    def to_dict(self):
        return {
            "id":         self.id,
            "date":       self.date.isoformat(),
            "index_name": self.index_name,
            "value":      self.value,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


class Projection(Base):
    __tablename__ = "projections"

    id               = Column(Integer, primary_key=True)
    projection_month = Column(Date, nullable=False)
    index_name       = Column(String(10), nullable=False)
    projected_value  = Column(Float, nullable=False)
    created_at       = Column(DateTime, default=datetime.utcnow)
    model_used       = Column(String(50), default="claude-sonnet-4-20250514")
    analysis         = Column(Text, nullable=True)
    risks            = Column(Text, nullable=True)

    def to_dict(self):
        import json
        return {
            "id":               self.id,
            "projection_month": self.projection_month.isoformat(),
            "index_name":       self.index_name,
            "projected_value":  self.projected_value,
            "created_at":       self.created_at.isoformat() if self.created_at else None,
            "model_used":       self.model_used,
            "analysis":         self.analysis,
            "risks":            json.loads(self.risks) if self.risks else [],
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    print(f"✓ Database ready  ({'SQLite: econ_dashboard.db' if IS_SQLITE else DATABASE_URL.split('@')[-1]})")


# TimescaleDB is a PostgreSQL-only extension — silently skipped on SQLite
TIMESCALE_SETUP_SQL = """
SELECT create_hypertable(
    'indices_history', 'date',
    if_not_exists => TRUE,
    migrate_data   => TRUE
);
"""
