"""
voice_chat.py — Voice endpoints for Raw Diet Personal Trainer chatbot.
Transcribes audio via Gemini, gets AI answer, returns TTS audio.
"""
import os, uuid, base64, json, logging, asyncio, traceback, re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, File, UploadFile, Depends, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, Response as FResponse
from sqlalchemy.orm import Session

from database import get_db_session
from models import Chat
from rag_engine import get_answer, get_answer_stream

# Import helpers from chat — using the exact names defined there
from chat import get_firebase_token, extract_uid, get_session, save_chat

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["Voice"])

# ── Client singletons ──────────────────────────────────────────────────────────

_gemini = None
_tts    = None

def _get_gemini():
    global _gemini
    if _gemini is None:
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY not set")
        _gemini = genai.Client(api_key=key)
    return _gemini

def _get_tts():
    global _tts
    if _tts is None:
        try:
            from google.cloud import texttospeech
            _tts = texttospeech.TextToSpeechClient()
            logger.info("✅ TTS client ready")
        except Exception as e:
            logger.warning(f"TTS not available: {e}")
    return _tts

# ── TTS ────────────────────────────────────────────────────────────────────────

def clean_tts(text: str) -> str:
    t = text.replace("[PDF_REQUESTED]","")
    t = re.sub(r"```[\s\S]*?```","",t)
    t = re.sub(r"`[^`]*`","",t)
    t = re.sub(r"https?://\S+","",t)
    t = re.sub(r"^\s{0,3}#{1,6}\s+","",t,flags=re.MULTILINE)
    t = re.sub(r"\*{1,3}([^*]+)\*{1,3}",r"\1",t)
    t = re.sub(r"^[\s]*[-*\u2022]\s+","",t,flags=re.MULTILINE)
    t = re.sub(r"[^\x00-\x7F]+"," ",t)
    return t.strip()

def tts(text: str) -> Optional[bytes]:
    try:
        from google.cloud import texttospeech
        client = _get_tts()
        if not client: return None
        if len(text) > 5000: text = text[:5000] + "..."
        inp   = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US", name="en-US-Neural2-F",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE)
        cfg   = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0, pitch=0.0)
        resp  = client.synthesize_speech(input=inp, voice=voice, audio_config=cfg)
        return resp.audio_content
    except Exception as e:
        logger.warning(f"TTS error: {e}")
        return None

# ── Transcription ──────────────────────────────────────────────────────────────

async def transcribe(audio: bytes, mime: str) -> str:
    from google.genai import types
    client = _get_gemini()
    result = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[
            "Transcribe this audio exactly as spoken. Return only the transcription, nothing else.",
            types.Part.from_bytes(data=audio, mime_type=mime or "audio/webm"),
        ])
    return result.text.strip()

# ── SSE helper ─────────────────────────────────────────────────────────────────

def voice_sse(text: str, sid: str, audio_b64: Optional[str] = None) -> StreamingResponse:
    def gen():
        yield f"data: {json.dumps({'type':'chunk','text':text})}\n\n"
        done = {"type":"done","text":text,"session_id":sid}
        if audio_b64: done["audio_base64"] = audio_b64
        yield f"data: {json.dumps(done)}\n\n"
    sr = StreamingResponse(gen(), media_type="text/event-stream",
         headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no",
                  "X-Session-ID":sid,"Access-Control-Expose-Headers":"X-Session-ID"})
    sr.set_cookie("session_id", sid, httponly=True,
                  max_age=365*24*3600, samesite="none", secure=True)
    return sr

# ── POST /voice/ — non-streaming ───────────────────────────────────────────────

@router.post("/")
async def voice_chat(
    request: Request, response: Response,
    file: UploadFile = File(...),
    session_id: str = Form(None),
    response_format: str = Form("json"),
    db: Session = Depends(get_db_session),
):
    try:
        sid   = get_session(request, response, session_id)
        token = get_firebase_token(request)
        uid   = extract_uid(token) if token else None
        audio = await file.read()

        user_text = await transcribe(audio, file.content_type)
        if not user_text:
            raise ValueError("Transcription returned empty")
        logger.info(f"🎤 Transcribed: {user_text[:60]}")

        ai_text = get_answer(question=user_text, session_id=sid,
                             db_session=db, firebase_token=token)
        save_chat(db, sid, uid, user_text, ai_text)

        audio_bytes = tts(clean_tts(ai_text))
        audio_b64   = base64.b64encode(audio_bytes).decode() if audio_bytes else None

        if response_format == "audio" and audio_bytes:
            return FResponse(content=audio_bytes, media_type="audio/mpeg",
                             headers={"Content-Disposition":"inline; filename=response.mp3",
                                      "Content-Length":str(len(audio_bytes))})
        return {"user_said":user_text,"message":ai_text,
                "audio_base64":audio_b64,"session_id":sid,"status":"success"}

    except Exception as e:
        logger.error(f"Voice POST error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Voice chat failed: {e}")

# ── POST /voice/stream — SSE streaming ────────────────────────────────────────

@router.post("/stream")
async def voice_stream(
    request: Request, response: Response,
    file: UploadFile = File(...),
    session_id: str = Form(None),
    db: Session = Depends(get_db_session),
):
    try:
        sid   = get_session(request, response, session_id)
        token = get_firebase_token(request)
        uid   = extract_uid(token) if token else None
        audio = await file.read()

        user_text = await transcribe(audio, file.content_type)
        if not user_text:
            def _err():
                yield f"data: {json.dumps({'type':'error','text':'Could not transcribe audio. Please try again.'})}\n\n"
            return StreamingResponse(_err(), media_type="text/event-stream")

        logger.info(f"🎤 Transcribed: {user_text[:60]}")
        _sid, _uid, _token, _utxt = sid, uid, token, user_text

        async def generate():
            final = ""
            try:
                async for chunk in _wrap(get_answer_stream(
                        question=_utxt, session_id=_sid,
                        db_session=db, firebase_token=_token)):
                    raw = chunk.strip()
                    if not raw.startswith("data:"): yield chunk; continue
                    try: evt = json.loads(raw[5:].strip())
                    except: yield chunk; continue

                    if evt.get("type") == "done":
                        final = evt.get("text","")
                        audio_bytes = tts(clean_tts(final))
                        audio_b64   = base64.b64encode(audio_bytes).decode() if audio_bytes else None
                        done = {"type":"done","text":final,"session_id":_sid,"user_said":_utxt}
                        if audio_b64: done["audio_base64"] = audio_b64
                        yield f"data: {json.dumps(done)}\n\n"
                    else:
                        yield chunk
            except Exception as e:
                logger.error(f"Voice stream error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type':'error','text':'Something went wrong. Please try again.'})}\n\n"
            finally:
                if final:
                    save_chat(db, _sid, _uid, _utxt, final)

        sr = StreamingResponse(generate(), media_type="text/event-stream",
             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no",
                      "Connection":"keep-alive","X-Session-ID":sid,
                      "Access-Control-Expose-Headers":"X-Session-ID"})
        sr.set_cookie("session_id", sid, httponly=True,
                      max_age=365*24*3600, samesite="none", secure=True)
        return sr

    except Exception as e:
        logger.error(f"Voice STREAM error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Voice stream failed: {e}")

async def _wrap(sync_gen):
    for item in sync_gen:
        yield item
        await asyncio.sleep(0)
