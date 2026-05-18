import json
import logging
import uuid
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db_session
from models import ChatMessage, ChatSession, MessageRole
from rag_engine import get_answer, get_answer_stream

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])


# ── Auth helpers ───────────────────────────────────────────────────────────────

def get_firebase_token(request: Request) -> Optional[str]:
    """Extract Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else None


def extract_firebase_uid(request: Request) -> Optional[str]:
    """
    Get Firebase UID — tries two sources:
    1. X-Firebase-UID header (set directly by frontend after verifying token)
    2. Decode JWT payload from Authorization Bearer token (no signature check)
    """
    uid = request.headers.get("X-Firebase-UID", "").strip()
    if uid:
        return uid
    token = get_firebase_token(request)
    if token:
        try:
            import base64, json as _j
            parts = token.split(".")
            if len(parts) == 3:
                pad     = parts[1] + "=" * (4 - len(parts[1]) % 4)
                payload = _j.loads(base64.urlsafe_b64decode(pad))
                return payload.get("uid") or payload.get("sub") or payload.get("user_id")
        except Exception:
            pass
    return None


def _set_cookie(response: Response, value: str) -> None:
    response.set_cookie("session_id", value, httponly=True,
                        max_age=365 * 24 * 3600, samesite="none", secure=True)


# ── User resolution ───────────────────────────────────────────────────────────

def get_prisma_user_id(db, firebase_uid: str) -> Optional[str]:
    """
    Resolve Firebase UID -> Prisma User.id.
    Queries: SELECT id FROM "User" WHERE "firebaseUid" = :uid
    """
    try:
        row = db.execute(
            text('SELECT id FROM "User" WHERE "firebaseUid" = :uid LIMIT 1'),
            {"uid": firebase_uid},
        ).fetchone()
        if row:
            logger.info(f"✅ Resolved Prisma user id: {row[0]}")
            return str(row[0])
    except Exception as e:
        logger.error(f"❌ User lookup error: {e}")
    logger.warning(f"⚠️ No User found for firebaseUid={firebase_uid}")
    return None


# ── Disease / Medical Check ───────────────────────────────────────────────────

# Keywords that indicate the user is asking for diet/health advice
HEALTH_ADVICE_KEYWORDS = [
    "diet", "meal", "food", "eat", "nutrition", "weight", "lose", "gain",
    "plan", "calorie", "protein", "fat", "carb", "breakfast", "lunch", "dinner",
    "snack", "recipe", "health", "fit", "exercise", "workout", "sugar", "cholesterol",
    "detox", "supplement", "vitamin", "mineral", "fiber", "keto", "vegan", "vegetarian",
    "slim", "muscle", "bulking", "cutting", "macro", "intake", "chart", "schedule",
]

DISEASE_KEYWORDS = [
    "diabetes", "diabetic", "bp", "blood pressure", "hypertension", "hypotension",
    "thyroid", "hypothyroid", "hyperthyroid", "heart", "kidney", "liver", "cancer",
    "pcod", "pcos", "cholesterol", "asthma", "arthritis", "gastric", "ibs",
    "crohn", "celiac", "anemia", "anaemia", "epilepsy", "disease", "condition",
    "disorder", "syndrome", "illness", "sick", "patient", "medicine", "medication",
    "tablet", "capsule", "insulin", "surgery", "operation", "hospital", "doctor",
    "treatment", "diagnosed", "suffering", "prescription",
]

NO_DISEASE_KEYWORDS = [
    "no disease", "no medical", "no condition", "no health issue", "no illness",
    "i am healthy", "i'm healthy", "i am fit", "i'm fit", "perfectly healthy",
    "fit and healthy", "completely healthy", "no problem", "no issue",
    "none", "nothing", "nope", "not at all", "i don't have",
    "i do not have", "no i don't", "no, i don't", "healthy person",
]

DISEASE_CHECK_QUESTION = (
    "Before I suggest anything, may I ask — "
    "**do you have any medical condition or health issue?** "
    "(e.g. diabetes, blood pressure, thyroid, etc.) \U0001f64f\n\n"
    "This helps me make sure any advice I give is safe for you."
)

DISEASE_DETECTED_RESPONSE = (
    "Since you have a medical condition, a diet plan must be designed carefully "
    "around your health history and medications.\n\n"
    "I strongly recommend consulting:\n\n"
    "\U0001f469\u200d\u2695\ufe0f **Dr. Meghana Kumare**\n"
    "Dietician & Sports Nutritionist | 20+ years experience\n"
    "Red Apple Wellness Diet Center\n\n"
    "\U0001f4de +91 7774944783\n"
    "\U0001f4e7 meghana17kumare@gmail.com\n"
    "\U0001f4cd Fortune Crest, Opp. Khare Town Post Office, Dharampeth, Nagpur - 440010\n"
    "\U0001f310 https://raw-diet.com/"
)


GREETING_KEYWORDS = [
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "yo", "hola", "greetings", "sup", "g'day", "howdy"
]

def is_greeting(message: str) -> bool:
    msg = message.lower().strip().replace("!", "").replace(".", "").replace("?", "").replace(",", "")
    if msg in GREETING_KEYWORDS:
        return True
    
    words = msg.split()
    if len(words) <= 3:
        extra_greeting_words = ["there", "bot", "chatbot", "friend", "everyone", "all", "buddy", "mate", "sir", "maam", "team", "you", "are", "how"]
        return all(w in GREETING_KEYWORDS or w in extra_greeting_words for w in words)
        
    return False


def clearly_no_disease(message: str) -> bool:
    msg_lower = message.lower().strip()
    # Normalize punctuation and common contractions
    normalized = msg_lower.replace("'", "").replace("`", "").replace("’", "")
    
    # Direct exact matches
    if normalized in ["no", "no.", "n", "none", "nothing", "nope", "nil", "normal", "healthy", "fine", "good", "no health issues"]:
        return True
    
    # Check keywords against both original and normalized
    no_disease_kws = NO_DISEASE_KEYWORDS + ["dont have", "dont have any", "no medical condition", "no health issue", "no health issues"]
    if any(kw in msg_lower for kw in no_disease_kws) or any(kw in normalized for kw in no_disease_kws):
        return True
        
    words = normalized.split()
    if "no" in words or "none" in words or "healthy" in words or "fit" in words:
        return True
        
    return False


def mentions_disease(message: str) -> bool:
    # If the message clearly indicates no disease, it shouldn't trigger the disease keywords
    if clearly_no_disease(message):
        return False
    return any(kw in message.lower() for kw in DISEASE_KEYWORDS)


def disease_already_checked(db, session_id: str, history: list) -> tuple:
    """
    Check if the disease check has already been performed in this session.
    First tries querying the database for all messages in the session,
    then falls back to the provided history list.
    Returns (already_asked: bool, user_has_disease: bool)
    """
    already_asked = False
    user_has_disease = False

    # 1. Try querying the database
    try:
        from sqlalchemy.orm import Session as SqlAlchemySession
        if db and (isinstance(db, SqlAlchemySession) or hasattr(db, "query")):
            messages = (
                db.query(ChatMessage)
                .filter(ChatMessage.sessionId == session_id)
                .order_by(ChatMessage.createdAt.asc())
                .all()
            )
            if messages:
                for m in messages:
                    # Bot asked the onboarding question
                    if m.role == MessageRole.ASSISTANT and ("medical condition" in m.content.lower() or "health issue" in m.content.lower()):
                        already_asked = True
                    # OR user explicitly declared no disease/healthy earlier
                    if m.role == MessageRole.USER and clearly_no_disease(m.content):
                        already_asked = True
                    # Check if user mentioned a disease
                    if m.role == MessageRole.USER and mentions_disease(m.content):
                        user_has_disease = True
                return already_asked, user_has_disease
    except Exception as e:
        logger.error(f"Database query in disease_already_checked failed: {e}")

    # 2. Fallback to provided history list
    for turn in history:
        bot_reply = turn.get("answer", "").lower()
        user_reply = turn.get("question", "").lower()
        if "medical condition" in bot_reply or "health issue" in bot_reply:
            already_asked = True
        if clearly_no_disease(user_reply):
            already_asked = True
        if mentions_disease(user_reply):
            user_has_disease = True

    return already_asked, user_has_disease


def disease_check_response(db, session_id: str, message: str, history: list) -> str | None:
    """
    Gate keeper — runs BEFORE the AI for every message.
    Returns a reply string to send to user, or None to let the AI proceed.

    Rules:
      1. Greeting message              -> let AI proceed (so the bot can welcome the user)
      2. Message mentions a disease    -> send DISEASE_DETECTED_RESPONSE
      3. Disease check already done
           a. User had no disease      -> let AI proceed
           b. User had a disease       -> send DISEASE_DETECTED_RESPONSE
      4. Check not done, user declares healthy -> let AI proceed
      5. Check not done yet            -> send DISEASE_CHECK_QUESTION
    """
    if is_greeting(message):
        return None

    if mentions_disease(message):
        return DISEASE_DETECTED_RESPONSE

    already_asked, has_disease = disease_already_checked(db, session_id, history)
    if already_asked:
        if has_disease:
            return DISEASE_DETECTED_RESPONSE
        return None

    if clearly_no_disease(message):
        return None

    return DISEASE_CHECK_QUESTION

# ── Session helpers ────────────────────────────────────────────────────────────

def get_or_create_session(db, firebase_uid: str, first_message: str = "") -> ChatSession:
    """
    Full flow:
      firebaseUid → Prisma User.id → find/create ChatSession

    first_message is used to set the session title immediately on creation.
    Raises 401 if firebase_uid is missing.
    Raises 404 if the user has not been created in the User table yet.
    """
    if not firebase_uid:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    prisma_user_id = get_prisma_user_id(db, firebase_uid)
    if not prisma_user_id:
        raise HTTPException(
            status_code=404,
            detail=(
                "User account not found. "
                "Please complete sign-up in the app before using the chatbot."
            ),
        )

    # Find the most recent active session for this user
    session = (
        db.query(ChatSession)
        .filter(ChatSession.userId == prisma_user_id, ChatSession.isActive == True)
        .order_by(ChatSession.createdAt.desc())
        .first()
    )

    if session is None:
        # Set title immediately from the first message so it's never NULL
        title = first_message[:255] if first_message else None
        session = ChatSession(userId=prisma_user_id, isActive=True, title=title)
        db.add(session)
        db.commit()
        db.refresh(session)
        logger.info(f"🆕 New ChatSession '{title}' for user {prisma_user_id}")

    return session


def save_exchange(db, session: ChatSession, user_message: str, assistant_reply: str) -> None:
    """
    Save USER + ASSISTANT messages.
    Also sets the session title from the first message if still NULL.
    """
    try:
        db.add(ChatMessage(
            sessionId=session.id,
            role=MessageRole.USER,
            content=user_message,
        ))
        db.add(ChatMessage(
            sessionId=session.id,
            role=MessageRole.ASSISTANT,
            content=assistant_reply,
        ))
        db.commit()
        logger.info(f"💾 Saved exchange in session {session.id}")
    except Exception as e:
        logger.warning(f"⚠️ save_exchange failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass


def get_recent_history(db, session_id, limit: int = 10) -> list[dict]:
    """Return the last N USER↔ASSISTANT pairs as dicts for RAG context."""
    try:
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.sessionId == session_id)
            .order_by(ChatMessage.createdAt.desc())
            .limit(limit * 2)
            .all()
        )
        rows = list(reversed(rows))
        history, i = [], 0
        while i < len(rows) - 1:
            if rows[i].role == MessageRole.USER and rows[i+1].role == MessageRole.ASSISTANT:
                history.append({"question": rows[i].content, "answer": rows[i+1].content})
                i += 2
            else:
                i += 1
        return history[-limit:]
    except Exception as e:
        logger.error(f"❌ get_recent_history error: {e}")
        return []


# ── SSE helpers ────────────────────────────────────────────────────────────────

def sse_wrap(text_: str, sid: str) -> StreamingResponse:
    def gen():
        yield f"data: {json.dumps({'type': 'chunk', 'text': text_})}\n\n"
        yield f"data: {json.dumps({'type': 'done',  'text': text_, 'session_id': sid})}\n\n"
    sr = StreamingResponse(gen(), media_type="text/event-stream",
         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                  "X-Session-ID": sid, "Access-Control-Expose-Headers": "X-Session-ID"})
    sr.set_cookie("session_id", sid, httponly=True,
                  max_age=365*24*3600, samesite="none", secure=True)
    return sr


async def _wrap_sync_gen(gen):
    """Run a sync generator in a thread pool so it doesn't block the event loop."""
    import concurrent.futures
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        for item in await loop.run_in_executor(pool, list, gen):
            yield item


# ── POST /chat/ — non-streaming ───────────────────────────────────────────────

@router.post("/")
async def chat_main(
    request:     Request,
    response:    Response,
    text:        str           = Form(...),
    session_id:  Optional[str] = Form(None),
    firebase_uid: Optional[str] = Form(None, description="Firebase UID (for testing via Swagger)"),
    db:          Session       = Depends(get_db_session),
):
    try:
        firebase_uid = firebase_uid or extract_firebase_uid(request)
        token        = get_firebase_token(request)
        msg          = text.strip()

        if not msg:
            raise HTTPException(400, "Message cannot be empty")

        chat_session = get_or_create_session(db, firebase_uid, first_message=msg)
        _set_cookie(response, str(chat_session.id))

        # Disease-check interception — runs before the AI
        history = get_recent_history(db, chat_session.id)
        intercept = disease_check_response(db, str(chat_session.id), msg, history)
        if intercept:
            save_exchange(db, chat_session, msg, intercept)
            return {"message": intercept, "session_id": str(chat_session.id), "status": "success"}

        reply = get_answer(
            question=msg,
            session_id=str(chat_session.id),
            db_session=db,
            firebase_token=token,
        )
        save_exchange(db, chat_session, msg, reply)

        return {"message": reply, "session_id": str(chat_session.id), "status": "success"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"chat_main error: {e}", exc_info=True)
        raise HTTPException(500, f"Chat failed: {e}")


# ── POST /chat/stream — SSE streaming ────────────────────────────────────────

@router.post("/stream")
async def chat_stream(
    request:      Request,
    response:     Response,
    text:         str           = Form(...),
    session_id:   Optional[str] = Form(None),
    firebase_uid: Optional[str] = Form(None, description="Firebase UID (for testing via Swagger)"),
    db:           Session       = Depends(get_db_session),
):
    try:
        firebase_uid = firebase_uid or extract_firebase_uid(request)
        token        = get_firebase_token(request)
        msg          = text.strip()

        if not msg:
            return sse_wrap("Please type a message first!", "anonymous")

        chat_session  = get_or_create_session(db, firebase_uid, first_message=msg)
        sid_str       = str(chat_session.id)
        _set_cookie(response, sid_str)

        # Disease-check interception — runs before the AI stream
        history      = get_recent_history(db, chat_session.id)
        intercept    = disease_check_response(db, sid_str, msg, history)
        if intercept:
            save_exchange(db, chat_session, msg, intercept)
            return sse_wrap(intercept, sid_str)

        async def generate():
            final = ""
            try:
                async for chunk in _wrap_sync_gen(get_answer_stream(
                    question=msg,
                    session_id=sid_str,
                    db_session=db,
                    firebase_token=token,
                )):
                    raw = chunk.strip()
                    if not raw.startswith("data:"):
                        yield chunk
                        continue
                    try:
                        evt = json.loads(raw[5:].strip())
                    except Exception:
                        yield chunk
                        continue

                    if evt.get("type") == "chunk":
                        final += evt.get("text", "")

                    if evt.get("type") == "done":
                        final = evt.get("text", final)
                        save_exchange(db, chat_session, msg, final)
                        yield f"data: {json.dumps({'type': 'done', 'text': final, 'session_id': sid_str})}\n\n"
                        return

                    yield chunk

            except Exception as e:
                logger.error(f"stream generate error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'text': 'Stream error, please retry.'})}\n\n"

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "X-Session-ID": sid_str,
                "Access-Control-Expose-Headers": "X-Session-ID",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"chat_stream error: {e}", exc_info=True)
        raise HTTPException(500, f"Stream failed: {e}")


# ── GET /chat/history ─────────────────────────────────────────────────────────

@router.get("/history")
async def chat_history(
    request: Request,
    db:      Session = Depends(get_db_session),
    limit:   int     = 20,
):
    """Return recent messages for the authenticated user's active session."""
    firebase_uid = extract_firebase_uid(request)
    if not firebase_uid:
        raise HTTPException(401, "Not authenticated")

    prisma_user_id = get_prisma_user_id(db, firebase_uid)
    if not prisma_user_id:
        raise HTTPException(404, "User not found")

    chat_session = (
        db.query(ChatSession)
        .filter(ChatSession.userId == prisma_user_id, ChatSession.isActive == True)
        .order_by(ChatSession.createdAt.desc())
        .first()
    )
    if not chat_session:
        return {"messages": [], "session_id": None}

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.sessionId == chat_session.id)
        .order_by(ChatMessage.createdAt.asc())
        .limit(limit)
        .all()
    )
    return {
        "session_id": str(chat_session.id),
        "messages": [
            {
                "id":        str(m.id),
                "role":      m.role.value,
                "content":   m.content,
                "metadata":  m.meta,
                "createdAt": m.createdAt.isoformat(),
            }
            for m in messages
        ],
    }
