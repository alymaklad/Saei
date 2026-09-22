"""Engine/session setup for the SQLite database."""
import os
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import config
from models import Base

os.makedirs("data", exist_ok=True)

# timeout=30: how long sqlite3 waits for a lock before raising
# "database is locked" (default is 0 -- fail instantly on any contention).
# This app routinely has multiple processes open on the same file at once
# (api.py + scheduler.py, both started by run.bat; a manual `python
# jobs/daily_run.py` on top of either) -- e.g. the dashboard's "Search now"
# button and the scheduler's own automatic search firing within the same
# few seconds. Without a timeout, that overlap surfaced as real
# sqlite3.OperationalError: database is locked failures on job inserts,
# even though nothing was actually wrong with the data.
engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """
    WAL journal mode lets one writer and any number of readers proceed
    concurrently (the default rollback-journal mode locks the whole file for
    any writer, which is what caused the "database is locked" failures this
    is fixing). busy_timeout=30000 is a second belt-and-suspenders layer on
    top of connect_args["timeout"] above -- some sqlite3/SQLAlchemy versions
    only honor one or the other depending on how the connection was opened,
    so both are set rather than relying on a single mechanism.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


# expire_on_commit=False: callers that build response data (or dicts) *after*
# the `with get_session()` block has exited still need attribute access on
# already-fetched rows. Without this, SQLAlchemy expires every object on
# commit and the next attribute read raises DetachedInstanceError.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _migrate_schema():
    """
    Base.metadata.create_all() only creates *missing tables* -- for a table
    that already exists (any real user's database, after the first run)
    it silently does nothing when models.py grows a new column, since SQLite
    has no "CREATE OR ALTER" equivalent create_all() can drive. Every time a
    new persisted field is added to a model, a matching ADD COLUMN needs to
    run here, guarded by checking PRAGMA table_info first so this stays a
    no-op on a database that already has the column (including a brand-new
    database, where create_all() above already created it with the full,
    current column set). SQLite's ALTER TABLE only supports ADD COLUMN --
    fine for this project so far, since no field has ever needed renaming or
    dropping.
    """
    additions = {
        "applications": {
            "ats_breakdown": "TEXT",
            "ats_explanation": "TEXT",
            "tailored_ats_score": "FLOAT",
            "tailored_ats_breakdown": "TEXT",
            "tailored_ats_explanation": "TEXT",
            "match_score": "FLOAT",
            "match_breakdown": "TEXT",
            "scoring_engine": "TEXT",
        },
    }
    with engine.connect() as conn:
        for table, columns in additions.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, coltype in columns.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        conn.commit()


def init_db():
    Base.metadata.create_all(engine)
    _migrate_schema()
    # Local import: avoids a module-load-time cycle (agents.search_agent only
    # imports db/models inside functions, not at module level, so this is
    # safe as long as it isn't hoisted to the top of this file).
    from agents.search_agent import seed_default_search_sites
    seed_default_search_sites()


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
