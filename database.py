"""
database.py — SQLAlchemy engine setup for Raw Diet chatbot.
Reads DATABASE_URL from env; runs without DB if not set.

IMPORTANT: All chatbot tables are created inside the 'chatbot' schema
so they are 100% isolated from Prisma-managed tables in the 'public' schema.
Prisma migrations will never touch anything in 'chatbot' schema.
"""
import os
import logging
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

# All chatbot tables live in this schema — completely separate from Prisma's public schema
CHATBOT_SCHEMA = "chatbot"

Base = declarative_base()

engine = None
SessionLocal = None

if DATABASE_URL:
    try:
        engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args={"connect_timeout": 10} if "postgresql" in DATABASE_URL else {},
        )

        # Create the chatbot schema if it doesn't exist yet
        # This runs once on startup — safe to run multiple times
        with engine.connect() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {CHATBOT_SCHEMA}"))
            conn.execute(text(f"SET search_path TO {CHATBOT_SCHEMA}"))
            conn.commit()
            logger.info(f"✅ Schema '{CHATBOT_SCHEMA}' ready")

        # Set search_path for every new connection so SQLAlchemy
        # always reads/writes to the chatbot schema automatically
        @event.listens_for(engine, "connect")
        def set_search_path(dbapi_conn, conn_record):
            cursor = dbapi_conn.cursor()
            cursor.execute(f"SET search_path TO {CHATBOT_SCHEMA}")
            cursor.close()

        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        logger.info("✅ Database engine created — using schema: chatbot")
    except Exception as e:
        logger.error(f"❌ DB engine creation failed: {e}")
        engine = None
        SessionLocal = None
else:
    logger.warning("⚠️ DATABASE_URL not set — running without persistence")


class _NoOpSession:
    """Stub session when DB is not configured — all operations are no-ops."""
    def query(self, *a, **kw): return self
    def filter(self, *a, **kw): return self
    def count(self): return 0
    def first(self): return None
    def all(self): return []
    def order_by(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def add(self, *a, **kw): pass
    def commit(self): pass
    def rollback(self): pass
    def refresh(self, *a, **kw): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


def get_db_session():
    """Yield a real or no-op DB session."""
    if SessionLocal:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    else:
        yield _NoOpSession()
