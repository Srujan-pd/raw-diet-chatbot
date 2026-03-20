"""
chat.py — Chat endpoints for Raw Diet Personal Trainer chatbot.
"""
import uuid, json, logging, asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Depends, Request, Response
from fastapi.responses import StreamingResponse, Response as FResponse
from sqlalchemy.orm import Session

from database import get_db_session
from models import Chat
from rag_engine import (get_answer, get_answer_stream,
                         generate_plan_pdf, generate_chat_pdf)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])


# ── Auth helpers ───────────────────────────────────────────────────────────────

def get_firebase_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else None


def extract_uid(token: str) -> Optional[str]:
    try:
        import base64, json as _j
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = _j.loads(base64.urlsafe_b64decode(pad))
        return payload.get("uid") or payload.get("sub") or payload.get("user_id")
    except Exception:
        return None


def get_session(request: Request, response: Response,
                body_sid: Optional[str]) -> str:
    """Use Firebase UID as session when available, else cookie/UUID."""
    token = get_firebase_token(request)
    if token:
        uid = extract_uid(token)
        if uid:
            response.set_cookie("session_id", uid, httponly=True,
                                max_age=365*24*3600, samesite="none", secure=True)
            return uid
    sid = body_sid or request.cookies.get("session_id") or str(uuid.uuid4())
    response.set_cookie("session_id", sid, httponly=True,
                        max_age=365*24*3600, samesite="none", secure=True)
    return sid


# ── SSE helper ─────────────────────────────────────────────────────────────────

def sse_wrap(text: str, sid: str) -> StreamingResponse:
    def gen():
        yield f"data: {json.dumps({'type':'chunk','text':text})}\n\n"
        yield f"data: {json.dumps({'type':'done','text':text,'session_id':sid})}\n\n"
    sr = StreamingResponse(gen(), media_type="text/event-stream",
         headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no",
                  "X-Session-ID":sid,"Access-Control-Expose-Headers":"X-Session-ID"})
    sr.set_cookie("session_id", sid, httponly=True,
                  max_age=365*24*3600, samesite="none", secure=True)
    return sr


def save_chat(db, sid: str, uid: Optional[str],
              question: str, answer: str):
    try:
        db.add(Chat(session_id=sid, firebase_uid=uid or sid,
                    question=question, answer=answer,
                    created_at=datetime.utcnow()))
        db.commit()
    except Exception as e:
        logger.warning(f"Chat save failed: {e}")
        try: db.rollback()
        except: pass


# ── POST /chat/ — non-streaming ────────────────────────────────────────────────

@router.post("/")
async def chat_main(
    request: Request, response: Response,
    text: str = Form(...),
    session_id: str = Form(None),
    db: Session = Depends(get_db_session),
):
    try:
        sid   = get_session(request, response, session_id)
        token = get_firebase_token(request)
        uid   = extract_uid(token) if token else None
        msg   = text.strip()
        if not msg:
            raise HTTPException(400, "Message cannot be empty")

        reply = get_answer(question=msg, session_id=sid,
                           db_session=db, firebase_token=token)
        save_chat(db, sid, uid, msg, reply)
        return {"message": reply, "session_id": sid, "status": "success"}

    except HTTPException: raise
    except Exception as e:
        logger.error(f"chat_main error: {e}", exc_info=True)
        raise HTTPException(500, f"Chat failed: {e}")


# ── POST /chat/stream — SSE streaming ─────────────────────────────────────────

@router.post("/stream")
async def chat_stream(
    request: Request, response: Response,
    text: str = Form(...),
    session_id: str = Form(None),
    db: Session = Depends(get_db_session),
):
    try:
        sid   = get_session(request, response, session_id)
        token = get_firebase_token(request)
        uid   = extract_uid(token) if token else None
        msg   = text.strip()

        if not msg:
            return sse_wrap("Please type a message first!", sid)

        # Capture in local vars for the generator closure
        _sid, _uid, _token, _msg = sid, uid, token, msg

        async def generate():
            final = ""
            try:
                async for chunk in _wrap(get_answer_stream(
                        question=_msg, session_id=_sid,
                        db_session=db, firebase_token=_token)):

                    # chunk is already a "data: {...}\n\n" SSE line
                    raw = chunk.strip()
                    if not raw.startswith("data:"):
                        yield chunk
                        continue

                    try:
                        evt = json.loads(raw[5:].strip())
                    except Exception:
                        yield chunk
                        continue

                    if evt.get("type") == "done":
                        final = evt.get("text", "")
                        # Inject session_id into done event
                        yield f"data: {json.dumps({'type':'done','text':final,'session_id':_sid})}\n\n"
                    elif evt.get("type") == "error":
                        yield chunk
                    else:
                        yield chunk   # chunk events pass through unchanged

            except Exception as e:
                logger.error(f"stream generator error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type':'error','text':'Something went wrong. Please try again.'})}\n\n"
            finally:
                if final:
                    save_chat(db, _sid, _uid, _msg, final)

        sr = StreamingResponse(generate(), media_type="text/event-stream",
             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no",
                      "Connection":"keep-alive","X-Session-ID":sid,
                      "Access-Control-Expose-Headers":"X-Session-ID"})
        sr.set_cookie("session_id", sid, httponly=True,
                      max_age=365*24*3600, samesite="none", secure=True)
        return sr

    except Exception as e:
        logger.error(f"chat_stream error: {e}", exc_info=True)
        raise HTTPException(500, f"Stream failed: {e}")


# ── GET /chat/history ──────────────────────────────────────────────────────────

@router.get("/history")
async def get_history(
    request: Request,
    session_id: str = None,
    limit: int = 50,
    db: Session = Depends(get_db_session),
):
    token = get_firebase_token(request)
    uid   = extract_uid(token) if token else None
    if not session_id:
        session_id = request.cookies.get("session_id")
    fval  = uid or session_id
    ffield= Chat.firebase_uid if uid else Chat.session_id
    if not fval:
        return []
    try:
        rows = (db.query(Chat).filter(ffield == fval)
                .order_by(Chat.created_at.asc()).limit(limit).all())
        return [{"id":r.id,"question":r.question,"answer":r.answer,
                 "created_at":r.created_at.isoformat() if r.created_at else None}
                for r in rows]
    except Exception as e:
        logger.error(f"History error: {e}")
        return []


# ── POST /chat/download-plan — branded diet plan PDF ─────────────────────────

@router.post("/download-plan")
async def download_plan(
    request: Request,
    plan_text: str = Form(...),
    user_name: str = Form("User"),
    plan_title: str = Form("Diet Plan"),
    db: Session = Depends(get_db_session),
):
    try:
        pdf = generate_plan_pdf(plan_text, user_name, plan_title)
        fname = f"RedApple_Plan_{user_name.replace(' ','_')}.pdf"
        return FResponse(content=pdf, media_type="application/pdf",
                         headers={"Content-Disposition": f'attachment; filename="{fname}"',
                                  "Content-Length": str(len(pdf))})
    except Exception as e:
        logger.error(f"Plan PDF error: {e}", exc_info=True)
        raise HTTPException(500, f"PDF failed: {e}")


# ── POST /chat/download-chat — full chat history PDF ─────────────────────────

@router.post("/download-chat")
async def download_chat(
    request: Request,
    user_name: str = Form("User"),
    session_id: str = Form(None),
    db: Session = Depends(get_db_session),
):
    try:
        token = get_firebase_token(request)
        uid   = extract_uid(token) if token else None
        if not session_id:
            session_id = request.cookies.get("session_id", "")
        fval  = uid or session_id
        ffield= Chat.firebase_uid if uid else Chat.session_id
        if not fval:
            raise HTTPException(400, "No session identified")

        rows = (db.query(Chat).filter(ffield == fval)
                .order_by(Chat.created_at.asc()).limit(200).all())
        history = [{"question":r.question,"answer":r.answer,
                    "created_at":r.created_at.isoformat() if r.created_at else ""}
                   for r in rows
                   if not (r.question.startswith("[") and r.question.endswith("]"))]
        if not history:
            raise HTTPException(404, "No chat history found")

        pdf = generate_chat_pdf(history, user_name)
        fname = f"RedApple_Chat_{user_name.replace(' ','_')}.pdf"
        return FResponse(content=pdf, media_type="application/pdf",
                         headers={"Content-Disposition": f'attachment; filename="{fname}"',
                                  "Content-Length": str(len(pdf))})
    except HTTPException: raise
    except Exception as e:
        logger.error(f"Chat PDF error: {e}", exc_info=True)
        raise HTTPException(500, f"Chat PDF failed: {e}")


# ── Async wrapper ──────────────────────────────────────────────────────────────

async def _wrap(sync_gen):
    for item in sync_gen:
        yield item
        await asyncio.sleep(0)
