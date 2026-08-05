"""Engine/session setup for the SQLite database."""
import os
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from models import Base

os.makedirs("data", exist_ok=True)

engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})

# expire_on_commit=False: callers that build response data (or dicts) *after*
# the `with get_session()` block has exited still need attribute access on
# already-fetched rows. Without this, SQLAlchemy expires every object on
# commit and the next attribute read raises DetachedInstanceError.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
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
