import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

load_dotenv()

logger       = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "")
Base         = declarative_base()
engine       = None
SessionLocal = None

if DATABASE_URL:
    try:
        engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,   # keeps NeonDB connection alive across auto-suspend
            pool_recycle=300,
            connect_args={"connect_timeout": 10},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            
        # Extract host for logging (safely, without password)
        try:
            from urllib.parse import urlparse
            parsed_url = urlparse(DATABASE_URL)
            db_host = parsed_url.hostname
            logger.info(f"✅ Connected to NeonDB (public schema) at host: {db_host}")
        except Exception:
            logger.info("✅ Connected to NeonDB (public schema)")
            
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    except Exception as e:
        logger.error(f":x: DB connection failed: {e}")
        engine = None
        SessionLocal = None
else:
    logger.warning(":warning: DATABASE_URL not set — running without persistence")


# ── No-op session (used when DB is unavailable) ───────────────────────────────

class _NoOpSession:
    def query(self, *a, **kw):      return self
    def filter(self, *a, **kw):     return self
    def filter_by(self, *a, **kw):  return self
    def execute(self, *a, **kw):    return self
    def fetchone(self):             return None
    def count(self):                return 0
    def first(self):                return None
    def all(self):                  return []
    def order_by(self, *a, **kw):   return self
    def limit(self, *a, **kw):      return self
    def add(self, *a, **kw):        pass
    def commit(self):               pass
    def rollback(self):             pass
    def refresh(self, *a, **kw):    pass
    def close(self):                pass
    def __enter__(self):            return self
    def __exit__(self, *a):         pass


def get_db_session():
    """FastAPI dependency — yields a real or no-op DB session."""
    if SessionLocal:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    else:
        yield _NoOpSession()

