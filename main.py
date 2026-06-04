import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from chat import router as chat_router
from rag_engine import initialize_gemini
from scheduler import run_daily_morning_messages

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Raw Diet Personal Trainer AI",
    description="AI-powered nutrition chatbot for Raw Diet app",
    version="1.0.0"
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── CORS ───────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
] or ["http://localhost:3000", "http://localhost:5173", "http://localhost:8081"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Firebase-UID"],
)

# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_tasks():
    logger.info("🥗 Starting Raw Diet Personal Trainer AI...")
    
    # Initialize Gemini
    if not initialize_gemini():
        logger.error("❌ Gemini initialization failed")
    else:
        logger.info("✅ Gemini initialized successfully")
    
    # Check database connection
    try:
        from database import engine, Base
        import models
        
        if engine is not None:
            logger.info("🔌 Testing database connection...")
            
            # Test connection with proper text() wrapper
            with engine.connect() as conn:
                # Simple connection test
                result = conn.execute(text("SELECT 1"))
                result.scalar()
                logger.info("✅ Database connection verified")
                
                # Check if ChatSession table exists
                result = conn.execute(text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'public' 
                        AND table_name = 'ChatSession'
                    )
                """))
                table_exists = result.scalar()
                
                if table_exists:
                    logger.info("✅ ChatSession table exists in public schema")
                    
                    # Count existing sessions
                    result = conn.execute(text('SELECT COUNT(*) FROM "ChatSession"'))
                    session_count = result.scalar()
                    logger.info(f"📊 Found {session_count} existing chat sessions")
                    
                    # Count existing messages
                    result = conn.execute(text('SELECT COUNT(*) FROM "ChatMessage"'))
                    message_count = result.scalar()
                    logger.info(f"📊 Found {message_count} existing chat messages")
                else:
                    logger.warning("⚠️ ChatSession table does NOT exist in public schema")
                    logger.warning("⚠️ Please run Prisma migrations to create the tables")
            
            logger.info("✅ Database setup complete")
        else:
            logger.warning("⚠️ No database engine — running without persistence")
            
    except Exception as e:
        logger.error(f"❌ Database startup failed: {e}", exc_info=True)
        logger.warning("⚠️ Continuing without database persistence")

# ── API Routers ────────────────────────────────────────────────────────────────
app.include_router(chat_router)

# ── Health Check Endpoint ─────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Health check endpoint for Cloud Run"""
    health_status = {
        "status": "healthy",
        "service": "Raw Diet Personal Trainer AI",
        "version": "1.0.0",
        "timestamp": __import__('datetime').datetime.utcnow().isoformat()
    }
    
    # Check database connectivity
    try:
        from database import engine
        from sqlalchemy import text
        
        if engine:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                health_status["database"] = "connected"
        else:
            health_status["database"] = "not_configured"
    except Exception as e:
        health_status["database"] = "error"
        health_status["database_error"] = str(e)
    
    return JSONResponse(health_status)


@app.get("/")
async def root():
    return JSONResponse({
        "status": "ok",
        "service": "Raw Diet Personal Trainer AI",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "chat": "/chat",
            "chat_stream": "/chat/stream",
            "history": "/chat/history",
            "sessions": "/chat/sessions",
            "docs": "/docs"
        }
    })


# ── Scheduler Endpoint ─────────────────────────────────────────────────────────

SCHEDULER_SECRET = os.getenv("SCHEDULER_SECRET", "")


@app.post("/scheduler/daily-morning")
async def daily_morning_scheduler(request: Request):
    """
    Called by Google Cloud Scheduler every day at 7:00 AM IST.
    Inserts a personalised morning message into each paid user's ChatSession.

    Security: Requires X-Scheduler-Secret header matching SCHEDULER_SECRET env var.
    Set this secret in Cloud Scheduler's request headers + Cloud Run env vars.

    Cloud Scheduler config:
      Schedule  : 30 1 * * *   (1:30 AM UTC = 7:00 AM IST)
      URL       : https://your-chatbot-url.run.app/scheduler/daily-morning
      Method    : POST
      Auth      : Add header  X-Scheduler-Secret: <your_secret_value>
    """
    # ── Secret check — prevents random people from triggering the scheduler ──
    if SCHEDULER_SECRET:
        incoming = request.headers.get("X-Scheduler-Secret", "")
        if incoming != SCHEDULER_SECRET:
            logger.warning("⚠️ Scheduler called with wrong secret")
            return JSONResponse(
                status_code=403,
                content={"error": "Forbidden"}
            )

    logger.info("⏰ Daily morning scheduler triggered")
    result = run_daily_morning_messages()
    return JSONResponse(result)


# ── Error handler ──────────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled exceptions"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc) if os.getenv("DEBUG") else "An error occurred"
        }
    )


# ── Shutdown events (optional) ─────────────────────────────────────────────────
@app.on_event("shutdown")
async def shutdown_tasks():
    """Clean up on shutdown"""
    logger.info("🛑 Shutting down Raw Diet Personal Trainer AI...")
    try:
        from database import engine
        if engine:
            engine.dispose()
            logger.info("✅ Database connections closed")
    except Exception as e:
        logger.error(f"❌ Error during shutdown: {e}")
