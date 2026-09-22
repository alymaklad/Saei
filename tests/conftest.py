"""
Pytest-wide setup.

Points DATABASE_URL at a throwaway file BEFORE any test module imports
config/db/api. This matters for two reasons:

  1. api.py calls init_db() at import time. Without this, merely importing
     api in a test opens the user's real data/job_agent.db -- and on some
     filesystems (a mounted volume, a network share) SQLite can't enable WAL
     there at all, so collection fails outright with "disk I/O error".
  2. Even where it works, a test suite has no business opening, migrating,
     or seeding the database somebody is actually using. Individual tests
     that need a database already build their own temp engine; this just
     guarantees the module-level import can never reach the real one.

conftest.py is imported before test modules, so setting the variable here
happens before config reads it.
"""
import os
import tempfile

_TEST_DB = os.path.join(tempfile.gettempdir(), "job_agent_pytest.db")

# Always override -- a DATABASE_URL inherited from the developer's shell or
# .env would defeat the point.
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
