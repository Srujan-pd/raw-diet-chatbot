import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from chat import router as chat_router
from rag_engine import initialize_gemini

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
    if not initialize_gemini():
        logger.error("❌ Gemini init failed")
    try:
        from database import engine, Base
        import models
        if engine is not None:
            Base.metadata.create_all(bind=engine)
            logger.info("✅ Database tables ready")
        else:
            logger.warning("⚠️ No DB — running without persistence")
    except Exception as e:
        logger.warning(f"⚠️ DB init skipped: {e}")

# ── API Routers ────────────────────────────────────────────────────────────────
app.include_router(chat_router)

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "Raw Diet Personal Trainer AI", "version": "1.0.0"}

@app.get("/")
async def root():
    return JSONResponse({"status": "ok", "service": "Raw Diet Personal Trainer AI"})

# ── Error handler ──────────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={
        "error": "Internal server error",
        "detail": str(exc) if os.getenv("DEBUG") else "An error occurred"
    })
