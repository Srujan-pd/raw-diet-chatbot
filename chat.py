"""
chat.py  —  Raw Diet Chatbot API

Key behaviour change:
  When a user asks for a diet plan / personalised plan / consultation,
  the bot returns a helpful teaser reply PLUS a `whatsapp_url` field.
  The frontend renders that URL as a "Chat on WhatsApp" button.

  The WhatsApp URL is a wa.me deep-link that pre-fills Meghana's number
  AND the user's full profile (from the database) as the message text.
  The user just taps "Send" — no typing required.

  No Twilio. No external API. Pure wa.me redirect.
"""

import json
import logging
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db_session, SessionLocal
from models import ChatMessage, ChatSession, MessageRole
from rag_engine import get_answer, get_answer_stream, fetch_user_profile
from whatsapp_redirect import build_whatsapp_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])


# ── Auth helpers ───────────────────────────────────────────────────────────────

def get_firebase_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else None


def extract_firebase_uid(request: Request) -> Optional[str]:
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


# ── User resolution ────────────────────────────────────────────────────────────

def get_prisma_user_id(db, firebase_uid: str) -> Optional[str]:
    try:
        row = db.execute(
            text('SELECT id FROM "User" WHERE "firebaseUid" = :uid LIMIT 1'),
            {"uid": firebase_uid},
        ).fetchone()
        if row:
            return str(row[0])
    except Exception as e:
        logger.error(f"❌ User lookup error: {e}")
    return None


# ── Disease check ──────────────────────────────────────────────────────────────

GREETING_KEYWORDS = {"hi", "hello", "hey", "good morning", "good afternoon",
                     "good evening", "yo", "hola", "greetings", "howdy",
                     "hiya", "namaste", "helo", "hii"}

# Keywords that indicate a WhatsApp CTA + URL should be attached to the response
WHATSAPP_CTA_KEYWORDS = [
    # Plan requests
    "diet plan", "meal plan", "diet chart", "food plan", "nutrition plan",
    "diet schedule", "what should i eat", "plan for me", "suggest a diet",
    "weight loss plan", "weight gain plan", "muscle plan", "fat loss plan",
    "give me a plan", "make me a plan", "create a plan", "custom plan",
    "personalised plan", "personalized plan",
    # Medical conditions → also need CTA
    "diabetes", "diabetic", "thyroid", "hypothyroid", "hyperthyroid",
    "pcos", "pcod", "hypertension", "blood pressure", "heart disease",
    "kidney disease", "liver disease", "cancer", "cholesterol",
    "arthritis", "anaemia", "anemia", "asthma", "epilepsy",
    "insulin", "chemotherapy", "dialysis", "post surgery",
    # Consultation / contact
    "consult", "consultation", "book", "appointment", "contact",
    "talk to doctor", "talk to dietician", "speak to meghana",
    "connect me", "whatsapp",
]


def is_greeting(message: str) -> bool:
    msg = message.lower().strip().rstrip("!.,?")
    if msg in GREETING_KEYWORDS:
        return True
    words = msg.split()
    extra = {"there", "bot", "friend", "everyone", "buddy", "mate", "sir", "maam", "guide"}
    return len(words) <= 3 and all(w in GREETING_KEYWORDS or w in extra for w in words)


def should_attach_whatsapp(message: str) -> bool:
    """Returns True when a WhatsApp CTA button should appear below the reply."""
    msg = message.lower()
    return any(kw in msg for kw in WHATSAPP_CTA_KEYWORDS)


# ── Session helpers ────────────────────────────────────────────────────────────

def get_or_create_session(db, firebase_uid: str, first_message: str = "") -> ChatSession:
    if not firebase_uid:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    prisma_user_id = get_prisma_user_id(db, firebase_uid)
    if not prisma_user_id:
        raise HTTPException(
            status_code=404,
            detail="User account not found. Please complete sign-up before using the chatbot.",
        )

    session = (
        db.query(ChatSession)
        .filter(ChatSession.userId == prisma_user_id, ChatSession.isActive == True)
        .order_by(ChatSession.createdAt.desc())
        .first()
    )

    if session is None:
        session = ChatSession(
            userId=prisma_user_id,
            isActive=True,
            title=first_message[:255] if first_message else None,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        logger.info(f"🆕 New ChatSession for user {prisma_user_id}")

    return session


def save_exchange(db, session: ChatSession, user_message: str, assistant_reply: str) -> None:
    try:
        db.add(ChatMessage(sessionId=session.id, role=MessageRole.USER,    content=user_message))
        db.add(ChatMessage(sessionId=session.id, role=MessageRole.ASSISTANT, content=assistant_reply))
        db.commit()
    except Exception as e:
        logger.warning(f"⚠️ save_exchange failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass


def get_recent_history(db, session_id, limit: int = 10) -> list[dict]:
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


# ── WhatsApp URL helper ────────────────────────────────────────────────────────

def get_whatsapp_url_for_request(msg: str, token: Optional[str]) -> Optional[str]:
    """
    Builds a wa.me URL pre-filled with the user's full profile.
    Returns None when the WhatsApp CTA is not relevant for this message.
    """
    if is_greeting(msg) or not should_attach_whatsapp(msg):
        return None
    try:
        profile = fetch_user_profile(token)
        return build_whatsapp_url(profile)
    except Exception as e:
        logger.warning(f"⚠️ Could not build WhatsApp URL: {e}")
        return None


# ── SSE helpers ────────────────────────────────────────────────────────────────

def sse_wrap(text_: str, sid: str, wa_url: Optional[str] = None) -> StreamingResponse:
    def gen():
        yield f"data: {json.dumps({'type': 'chunk', 'text': text_})}\n\n"
        done_payload = {"type": "done", "text": text_, "session_id": sid}
        if wa_url:
            done_payload["whatsapp_url"] = wa_url
        yield f"data: {json.dumps(done_payload)}\n\n"

    sr = StreamingResponse(gen(), media_type="text/event-stream",
         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                  "X-Session-ID": sid, "Access-Control-Expose-Headers": "X-Session-ID"})
    sr.set_cookie("session_id", sid, httponly=True,
                  max_age=365*24*3600, samesite="none", secure=True)
    return sr


async def _wrap_sync_gen(gen):
    import concurrent.futures
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        for item in await loop.run_in_executor(pool, list, gen):
            yield item


# ── POST /chat/ — non-streaming ───────────────────────────────────────────────

@router.post("/")
async def chat_main(
    request:      Request,
    response:     Response,
    text:         str           = Form(...),
    session_id:   Optional[str] = Form(None),
    firebase_uid: Optional[str] = Form(None),
    db:           Session       = Depends(get_db_session),
):
    """
    Returns JSON:
    {
      "message":       "<assistant's reply>",
      "session_id":    "<uuid>",
      "status":        "success",
      "whatsapp_url":  "https://wa.me/917774944783?text=..."   ← present only when relevant
    }

    Frontend behaviour:
      - Render `message` as the chat bubble (assistant's reply)
      - If `whatsapp_url` is present → show a green "Chat on WhatsApp" button below the bubble
      - Tapping the button opens WhatsApp with Dr. Meghana's number pre-selected
        and the user's full profile already typed — user just taps Send
    """
    try:
        firebase_uid = firebase_uid or extract_firebase_uid(request)
        token        = get_firebase_token(request)
        msg          = text.strip()

        if not msg:
            raise HTTPException(400, "Message cannot be empty")

        chat_session = get_or_create_session(db, firebase_uid, first_message=msg)
        _set_cookie(response, str(chat_session.id))
        # ── Get AI reply ──────────────────────────────────────────────────────
        reply = get_answer(
            question=msg,
            session_id=str(chat_session.id),
            db_session=db,
            firebase_token=token,
        )
        save_exchange(db, chat_session, msg, reply)

        # ── Attach WhatsApp URL when the topic warrants it ────────────────────
        wa_url = get_whatsapp_url_for_request(msg, token)
        result = {
            "message":    reply,
            "session_id": str(chat_session.id),
            "status":     "success",
        }
        if wa_url:
            result["whatsapp_url"] = wa_url
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"chat_main error: {e}", exc_info=True)
        raise HTTPException(500, f"Chat failed: {e}")


# ── POST /chat/stream — SSE streaming ─────────────────────────────────────────

@router.post("/stream")
async def chat_stream(
    request:      Request,
    response:     Response,
    text:         str           = Form(...),
    session_id:   Optional[str] = Form(None),
    firebase_uid: Optional[str] = Form(None),
    db:           Session       = Depends(get_db_session),
):
    """
    Streams SSE chunks. The final `done` event includes `whatsapp_url` when relevant:
      data: {"type": "done", "text": "...", "session_id": "...", "whatsapp_url": "https://wa.me/..."}

    Frontend should render the WhatsApp button when it sees `whatsapp_url` in the done event.
    """
    try:
        firebase_uid = firebase_uid or extract_firebase_uid(request)
        token        = get_firebase_token(request)
        msg          = text.strip()

        if not msg:
            return sse_wrap("Hey, looks like your message was empty — what's on your mind? 😊", "anonymous")

        chat_session = get_or_create_session(db, firebase_uid, first_message=msg)
        sid_str      = str(chat_session.id)
        _set_cookie(response, sid_str)

        # Pre-compute WhatsApp URL (based on message topic)
        wa_url = get_whatsapp_url_for_request(msg, token)

        # Stream AI's answer
        async def generate():
            from database import _NoOpSession
            db_session = SessionLocal() if SessionLocal else _NoOpSession()
            final = ""
            try:
                async for chunk in _wrap_sync_gen(get_answer_stream(
                    question=msg,
                    session_id=sid_str,
                    db_session=db_session,
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
                        yield chunk

                    elif evt.get("type") == "done":
                        final = evt.get("text", final)
                        save_exchange(db_session, chat_session, msg, final)
                        done_payload = {
                            "type":       "done",
                            "text":       final,
                            "session_id": sid_str,
                        }
                        if wa_url:
                            done_payload["whatsapp_url"] = wa_url
                        yield f"data: {json.dumps(done_payload)}\n\n"
                        return
                    else:
                        yield chunk

            except Exception as e:
                logger.error(f"stream generate error: {e}", exc_info=True)
                err_payload = json.dumps({'type': 'error', 'text': "Something came up on my end — let's try that again! 💪"})
                yield f"data: {err_payload}\n\n"
            finally:
                if SessionLocal and db_session:
                    db_session.close()

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


# ── GET /chat/history ──────────────────────────────────────────────────────────

@router.get("/history")
async def chat_history(
    request: Request,
    db:      Session = Depends(get_db_session),
    limit:   int     = 20,
):
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

