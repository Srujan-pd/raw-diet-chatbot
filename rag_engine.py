"""
rag_engine.py — AI answer engine for Raw Diet Personal Trainer chatbot.

Uses Gemini as the core LLM. The "knowledge base" here is the user's profile
fetched from the Raw Diet backend API (identity, health conditions, food activity).
No vector store / FAISS required — all context is assembled dynamically from:
  1. User profile from Raw Diet API (via Firebase UID passed in headers)
  2. General nutrition & fitness knowledge baked into the Gemini prompt
  3. Conversation history for multi-turn context
"""

import os
import logging
import traceback
import re
import json
from typing import Generator, Optional

from google import genai
from dotenv import load_dotenv
try:
    from pdf_generator import build_plan_pdf, build_chat_pdf
    _PDF_GEN_OK = True
except Exception as _pdf_err:
    _PDF_GEN_OK = False
    build_plan_pdf = None
    build_chat_pdf = None

load_dotenv()
logger = logging.getLogger(__name__)

# ── Gemini client singleton ────────────────────────────────────────────────────
gemini_client = None


def initialize_gemini() -> bool:
    global gemini_client
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment")
        gemini_client = genai.Client(api_key=api_key)
        logger.info("✅ Gemini client initialized")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to initialize Gemini: {e}")
        return False


# ── Raw Diet backend API helpers ───────────────────────────────────────────────

RAW_DIET_API_BASE = os.getenv("RAW_DIET_API_BASE", "https://test---raw-diet-backend-5rnsarrnya-uc.a.run.app")


def fetch_user_profile(firebase_token: Optional[str]) -> Optional[dict]:
    """
    Fetch the user's full profile from the Raw Diet backend using their Firebase JWT.
    Returns None if token is missing or request fails.
    """
    if not firebase_token:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{RAW_DIET_API_BASE}/api/users/me",
            headers={"Authorization": f"Bearer {firebase_token}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            logger.info(f"✅ Fetched user profile for {data.get('email', 'unknown')}")
            return data
    except Exception as e:
        logger.warning(f"⚠️ Could not fetch user profile: {e}")
        return None


def build_user_context(profile: Optional[dict]) -> str:
    """Convert a user profile dict to a plain-text context block for the prompt."""
    if not profile:
        return "No user profile available. Answer as a general diet and fitness expert."

    lines = ["=== USER PROFILE ==="]

    name = profile.get("name") or "the user"
    lines.append(f"Name: {name}")

    identity = profile.get("identity") or {}
    if identity:
        if identity.get("age"):
            lines.append(f"Age: {identity['age']} years")
        if identity.get("gender"):
            lines.append(f"Gender: {identity['gender']}")
        if identity.get("heightCm"):
            lines.append(f"Height: {identity['heightCm']} cm")
        if identity.get("weightKg"):
            lines.append(f"Weight: {identity['weightKg']} kg")
            # Estimate BMI
            h_m = identity["heightCm"] / 100
            bmi = round(identity["weightKg"] / (h_m * h_m), 1)
            lines.append(f"BMI: {bmi} (calculated)")

    food = profile.get("foodactivity") or {}
    if food:
        if food.get("foodPreferences"):
            lines.append(f"Diet type: {', '.join(food['foodPreferences'])}")
        if food.get("activityLevel"):
            lines.append(f"Activity level: {food['activityLevel'].replace('_', ' ')}")
        if food.get("allergies"):
            lines.append(f"Allergies / intolerances: {', '.join(food['allergies'])}")

    health = profile.get("health") or {}
    if health:
        if health.get("conditions"):
            lines.append(f"Health conditions: {', '.join(health['conditions'])}")
        if health.get("otherDetails"):
            lines.append(f"Other health notes: {health['otherDetails']}")

    allergies = profile.get("allergies")
    if allergies and not food.get("allergies"):
        lines.append(f"Allergies: {', '.join(allergies)}")

    diet_prefs = profile.get("Diet")
    if diet_prefs and not food.get("foodPreferences"):
        lines.append(f"Diet preference: {', '.join(diet_prefs)}")

    lines.append("===================")
    return "\n".join(lines)


# ── System prompt ──────────────────────────────────────────────────────────────

CLINIC_INFO = """
=== RED APPLE WELLNESS DIET CENTER ===
Website : https://raw-diet.com/
Tagline : RAW-DIET | SINCE 2008

Founder & Director:
  Dr. Meghana Kumare
  Dietician & Sports Nutritionist | 20+ years experience
  MSc - Dietetics / Nutrition | PG Diploma in Dietetics
  Certificate Course - Specialist in Sports Nutrition (ISSA, 2017)
  Owner – Red Apple Wellness Diet Center (Since 2008)
  Founder – MK Fit Foods
  Centers in Nagpur, Mumbai, and Dubai

Specialisations:
  Weight Loss | Weight Gain | Muscle Building | Sports Nutrition
  Diabetes | Hypertension | Hypothyroidism | PCOS/PCOD
  Heart Disease | Kidney Disease | Cancer Diet | Pregnancy & Lactation
  Kids Nutrition | Clinical Nutrition | Detox Diet

Contact:
  Phone   : +91 7774944783
  Email   : rawdiets12@gmail.com
  Address : Fortune Crest, Opp. Khare Town Post Office,
            Dharampeth, Nagpur – 440010
=======================================
"""

SYSTEM_PROMPT = f"""You are an AI diet and nutrition assistant for **Red Apple Wellness Diet Center** — founded by Dr. Meghana Kumare in 2008. You assist users of the Raw Diet app, which is the official app of this center.

{CLINIC_INFO}

YOUR ROLE:
You act as a warm, knowledgeable nutrition assistant representing Red Apple Wellness Diet Center. You help users with:
- Personalised diet and meal plans (weight loss, weight gain, muscle building)
- Meal suggestions based on diet type (veg / vegan / non-veg / jain / eggetarian)
- Calorie and macronutrient guidance based on the user's body stats
- Understanding nutrition labels, ingredients, and food choices
- General healthy eating habits and lifestyle tips
- Post-meal digestive issues (e.g. "I ate X and feel Y — what happened?")
- Hydration, recovery nutrition, and clean eating principles
- Explaining the philosophy of Raw Diet / whole food nutrition
- Directing users to Dr. Meghana Kumare and the clinic for personalised consultation

TONE & STYLE:
- Warm, motivating, and supportive — like a caring nutrition advisor
- Be specific and actionable — give real meal names, portions, timings
- Personalise responses based on the user's profile if available
- Use simple, friendly language — explain terms when you use them
- Celebrate small wins; be empathetic about struggles
- Always represent Red Apple Wellness Diet Center professionally

══════════════════════════════════════════════════════
MEDICAL SAFETY RULES — STRICTLY ENFORCED — NO EXCEPTIONS
══════════════════════════════════════════════════════

The following are MEDICAL topics. When ANY of these are mentioned — whether the user asks for a diet plan, exercises, tips, or anything related — you MUST use the REDIRECT RESPONSE below. Do NOT provide clinical dietary plans for these conditions.

MEDICAL CONDITIONS (triggers for redirect):
Diabetes | High Blood Pressure | Low BP | Hypertension | Hypotension | Heart Disease |
Cholesterol | Thyroid | Hypothyroid | Hyperthyroid | PCOS | PCOD | Kidney Disease |
Liver Disease | Cancer | Arthritis | Anaemia | Asthma | Epilepsy | Any chronic illness |
Any diagnosed medical condition | Any prescribed medication | Post-surgery diet |
Chemotherapy | Dialysis | Any supplement that interacts with drugs

REDIRECT RESPONSE — Use this format when any above condition is mentioned:
"I understand your concern about [condition]. Since this involves a medical condition, a safe diet plan must be designed around your specific medications, test reports, and health history.

For a personalised and medically safe diet plan, I strongly recommend consulting:

👩‍⚕️ **Dr. Meghana Kumare**
Dietician & Sports Nutritionist | 20+ years experience
Red Apple Wellness Diet Center

📞 **+91 7774944783**
📧 **rawdiets12@gmail.com**
📍 Fortune Crest, Opp. Khare Town Post Office, Dharampeth, Nagpur – 440010
🌐 https://raw-diet.com/

Dr. Meghana specialises in clinical nutrition for [condition] and will create a plan tailored to your specific needs and medications."

EMERGENCY RULE: If user reports chest pain, difficulty breathing, severe dizziness, or loss of consciousness — say: "⚠️ Please call emergency services or go to the nearest hospital immediately. This needs urgent medical attention."

MEDICATION RULE: NEVER name, suggest, or discuss any medication or dosage under any circumstances.

══════════════════════════════════════════════════════

PDF PLAN INSTRUCTIONS — IMPORTANT:
- Whenever you provide ANY structured diet plan or meal plan (whether or not the user asked for PDF) — ALWAYS add [PDF_REQUESTED] on its own line at the very end of your response.
- This allows the user to download a branded PDF. Only add it to structured plans, not general answers.

OTHER RULES:
- NEVER suggest anything that contradicts the user's known allergies
- Stay on topic: diet, nutrition, fitness, healthy eating only
- If asked anything unrelated (tech support, coding, politics etc.) politely redirect
- Do NOT mention other diet centers, competitors, or other nutritionists
"""


def build_prompt(
    user_message: str,
    profile: Optional[dict],
    chat_history: list,
    goal_hint: Optional[str] = None,
) -> str:
    """Build the full prompt sent to Gemini."""
    user_ctx = build_user_context(profile)
    goal_line = f"\nUser's stated goal: {goal_hint}" if goal_hint else ""

    # Format recent chat history
    history_block = ""
    if chat_history:
        turns = []
        for msg in chat_history[-10:]:   # last 10 turns max
            turns.append(f"User: {msg.get('question', '')}")
            turns.append(f"Trainer: {msg.get('answer', '')}")
        history_block = "\n=== RECENT CONVERSATION ===\n" + "\n".join(turns) + "\n===========================\n"

    return f"""{SYSTEM_PROMPT}

{user_ctx}{goal_line}
{history_block}
User message: {user_message}

Trainer response:"""


# ── Detect query category ──────────────────────────────────────────────────────

def detect_goal_from_history(history: list) -> Optional[str]:
    """Try to detect the user's stated fitness goal from recent messages."""
    goal_keywords = {
        "weight loss": ["lose weight", "weight loss", "slim down", "fat loss", "cut"],
        "weight gain": ["gain weight", "bulk", "weight gain", "gain mass"],
        "muscle building": ["build muscle", "muscle gain", "strength", "muscle building", "bulk up"],
        "maintenance": ["maintain", "stay fit", "healthy lifestyle", "eat healthy"],
    }
    for msg in reversed(history[-20:] if history else []):
        text = (msg.get("question", "") + " " + msg.get("answer", "")).lower()
        for goal, keywords in goal_keywords.items():
            if any(kw in text for kw in keywords):
                return goal
    return None


def is_greeting(text: str) -> bool:
    greetings = {"hi", "hello", "hey", "good morning", "good afternoon", "good evening",
                 "hiya", "howdy", "namaste", "helo", "hii", "yo"}
    t = text.lower().strip().rstrip("!.,")
    return t in greetings or any(t.startswith(g + " ") for g in greetings)


# ── Main answer functions ──────────────────────────────────────────────────────

def get_answer(
    question: str,
    session_id: Optional[str] = None,
    db_session=None,
    firebase_token: Optional[str] = None,
) -> str:
    """Blocking answer — fetches user profile, builds prompt, calls Gemini."""
    global gemini_client

    if gemini_client is None:
        return "AI service is not available right now. Please try again shortly."

    if is_greeting(question):
        return (
            "Hello! 👋 Welcome to **Red Apple Wellness Diet Center** — your personal nutrition assistant! 🍎\n\n"
            "I'm here to help you with diet plans, meal ideas, nutrition guidance, and health goals.\n\n"
            "Whether you want to **lose weight**, **gain weight**, **build muscle**, or just eat healthier — I'm here for you! 💪\n\n"
            "What would you like help with today?"
        )

    try:
        # Fetch user profile
        profile = fetch_user_profile(firebase_token)

        # Fetch chat history
        history = []
        if db_session and session_id:
            try:
                history = get_recent_messages(db_session, session_id, limit=10)
                history = [{"question": c.question, "answer": c.answer} for c in history]
            except Exception as e:
                logger.warning(f"Could not load history: {e}")

        goal = detect_goal_from_history(history)
        prompt = build_prompt(question, profile, history, goal)

        logger.info("🤖 Calling Gemini for answer...")
        resp = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        answer = resp.text.strip()
        logger.info(f"✅ Answer ready ({len(answer)} chars)")
        return answer

    except Exception as e:
        logger.error(f"❌ get_answer error: {e}\n{traceback.format_exc()}")
        return (
            "I encountered an issue while preparing your answer. "
            "Please try again — I'm here to help with your diet and fitness! 💪"
        )


def get_answer_stream(
    question: str,
    session_id: Optional[str] = None,
    db_session=None,
    firebase_token: Optional[str] = None,
) -> Generator[str, None, None]:
    """
    Streaming answer generator.
    Yields SSE lines:
        data: {"type": "chunk", "text": "..."}   ← raw Gemini token
        data: {"type": "done",  "text": "..."}   ← full final answer
        data: {"type": "error", "text": "..."}   ← on failure
    """
    import json as _j

    def sse(payload: dict) -> str:
        return f"data: {_j.dumps(payload)}\n\n"

    if gemini_client is None:
        msg = "AI service is not available right now. Please try again shortly."
        yield sse({"type": "chunk", "text": msg})
        yield sse({"type": "done",  "text": msg})
        return

    if is_greeting(question):
        msg = (
            "Hello! 👋 Welcome to **Red Apple Wellness Diet Center** — your personal nutrition assistant! 🍎\n\n"
            "I'm here to help you with diet plans, meal ideas, nutrition guidance, and health goals.\n\n"
            "Whether you want to **lose weight**, **gain weight**, **build muscle**, or just eat healthier — I'm here for you! 💪\n\n"
            "What would you like help with today?"
        )
        yield sse({"type": "chunk", "text": msg})
        yield sse({"type": "done",  "text": msg})
        return

    try:
        profile = fetch_user_profile(firebase_token)

        history = []
        if db_session and session_id:
            try:
                history = get_recent_messages(db_session, session_id, limit=10)
                history = [{"question": c.question, "answer": c.answer} for c in history]
            except Exception as e:
                logger.warning(f"Could not load history: {e}")

        goal = detect_goal_from_history(history)
        prompt = build_prompt(question, profile, history, goal)

        logger.info("🤖 Streaming from Gemini...")
        accumulated = ""

        stream = gemini_client.models.generate_content_stream(
            model="gemini-2.0-flash",
            contents=prompt
        )

        for chunk in stream:
            if chunk.text:
                accumulated += chunk.text
                yield sse({"type": "chunk", "text": chunk.text})

        logger.info(f"✅ Stream complete ({len(accumulated)} chars)")
        yield sse({"type": "done", "text": accumulated.strip()})

    except Exception as e:
        logger.error(f"❌ get_answer_stream error: {e}\n{traceback.format_exc()}")
        err_msg = "I encountered an issue — please try again! 💪"
        yield sse({"type": "error", "text": err_msg})



# ── PDF Generation ─────────────────────────────────────────────────────────────

def _clean_markdown_for_pdf(text: str) -> str:
    """Strip markdown symbols for clean PDF text."""
    import re
    text = text.replace("[PDF_REQUESTED]", "").strip()
    # Bold markers → keep text, remove **
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    return text


def _parse_lines_for_pdf(raw_text: str):
    """
    Parse markdown-like text into a list of (type, text) tuples.
    Types: heading1, heading2, heading3, bullet, numbered, body, empty
    """
    import re
    lines = raw_text.replace("[PDF_REQUESTED]", "").split("\n")
    parsed = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            parsed.append(("empty", ""))
            continue
        # Headings
        if stripped.startswith("### "):
            parsed.append(("heading3", stripped[4:].strip()))
        elif stripped.startswith("## "):
            parsed.append(("heading2", stripped[3:].strip()))
        elif stripped.startswith("# "):
            parsed.append(("heading1", stripped[2:].strip()))
        # Bold-only line = treat as heading
        elif re.match(r"^\*\*(.+)\*\*$", stripped) or re.match(r"^__(.+)__$", stripped):
            inner = re.sub(r"^\*\*|\*\*$|^__|__$", "", stripped)
            parsed.append(("heading3", inner.strip()))
        # Numbered list
        elif re.match(r"^\d+[\.\)]\s", stripped):
            parsed.append(("numbered", stripped))
        # Bullet
        elif stripped.startswith(("- ", "* ", "• ", "· ")):
            parsed.append(("bullet", stripped[2:].strip()))
        else:
            # Inline bold removal
            clean = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)
            clean = re.sub(r"\*(.+?)\*", r"\1", clean)
            parsed.append(("body", clean))
    return parsed



def _make_pdf_styles():
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
    from reportlab.lib import colors
    C_RED=colors.HexColor("#B03A2E"); C_RED_DARK=colors.HexColor("#7B241C")
    C_GREEN=colors.HexColor("#1A5276"); C_DARK=colors.HexColor("#1C2833")
    C_MID=colors.HexColor("#566573"); C_WHITE=colors.white
    b=getSampleStyleSheet()["Normal"]
    def S(n,**k): return ParagraphStyle(n,parent=b,**k)
    return dict(
        h1=S("h1",fontName="Helvetica-Bold",fontSize=12,textColor=C_WHITE,leading=16),
        h2=S("h2",fontName="Helvetica-Bold",fontSize=10.5,textColor=C_GREEN,leading=14,spaceBefore=8,spaceAfter=2),
        h3=S("h3",fontName="Helvetica-Bold",fontSize=9.5,textColor=C_DARK,leading=13,spaceBefore=5,spaceAfter=2),
        body=S("body",fontName="Helvetica",fontSize=9,textColor=C_DARK,leading=14,spaceAfter=3,alignment=TA_JUSTIFY),
        bullet_val=S("bv",fontName="Helvetica",fontSize=9,textColor=C_DARK,leading=13),
        numbered=S("num",fontName="Helvetica",fontSize=9,textColor=C_DARK,leading=13,spaceAfter=2,leftIndent=14),
        meta_label=S("ml",fontName="Helvetica-Bold",fontSize=8.5,textColor=C_GREEN,leading=12),
        meta_value=S("mv",fontName="Helvetica",fontSize=8.5,textColor=C_DARK,leading=12),
        footer=S("ft",fontName="Helvetica",fontSize=7.5,textColor=C_MID,alignment=TA_CENTER,leading=11),
        disclaimer=S("dc",fontName="Helvetica-Oblique",fontSize=8,textColor=C_RED_DARK,alignment=TA_CENTER,leading=12),
    )


def _parse_md(text):
    import re
    text = text.replace("[PDF_REQUESTED]","").strip()
    result = []
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            result.append(("empty",""))
        elif s.startswith("### "):
            result.append(("h3", s[4:].strip()))
        elif s.startswith("## "):
            result.append(("h2", s[3:].strip()))
        elif s.startswith("# "):
            result.append(("h1", s[2:].strip()))
        elif re.match(r"^\*\*[^*]+\*\*$", s):
            result.append(("h3", re.sub(r"\*\*","",s).strip()))
        elif re.match(r"^\d+[\.\)]\s", s):
            c = re.sub(r"^\d+[\.\)]\s+","",s)
            c = re.sub(r"\*\*(.+?)\*\*",r"\1",c)
            result.append(("numbered", s[:2]+c))
        elif s.startswith(("- ","* ","• ","· ")):
            c = re.sub(r"\*\*(.+?)\*\*",r"\1",s[2:].strip())
            result.append(("bullet", c))
        else:
            c = re.sub(r"\*\*(.+?)\*\*",r"\1",s)
            c = re.sub(r"\*(.+?)\*",r"\1",c)
            result.append(("body", c))
    return result


def _x(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def generate_plan_pdf(plan_text: str, user_name: str = "User",
                      plan_title: str = "Diet Plan") -> bytes:
    """Generate branded diet plan PDF using pdf_generator module."""
    if _PDF_GEN_OK and build_plan_pdf:
        try:
            return build_plan_pdf(plan_text, user_name, plan_title)
        except Exception as e:
            logger.error(f"build_plan_pdf error: {e}\n{traceback.format_exc()}")
    return _simple_text_pdf(plan_text, user_name)


def generate_chat_pdf(history: list, user_name: str = "User") -> bytes:
    """Generate branded chat history PDF using pdf_generator module."""
    if _PDF_GEN_OK and build_chat_pdf:
        try:
            return build_chat_pdf(history, user_name)
        except Exception as e:
            logger.error(f"build_chat_pdf error: {e}\n{traceback.format_exc()}")
    # Fallback: simple text PDF
    text = "\n\n".join([
        f"Q: {m.get('question','')}\nA: {m.get('answer','')}"
        for m in history
    ])
    return _simple_text_pdf(text, user_name)


def _simple_text_pdf(plan_text: str, user_name: str) -> bytes:
    """Pure-Python fallback — generates a valid PDF-1.4 without any third-party lib."""
    from datetime import datetime
    import io, re
    clean = plan_text.replace("[PDF_REQUESTED]","").strip()
    today = datetime.now().strftime("%d %B %Y")
    lines = [
        "Red Apple Wellness Diet Center",
        "RAW-DIET | SINCE 2008  -  Dr. Meghana Kumare",
        "+91 7774944783 | rawdiets12@gmail.com | raw-diet.com",
        "="*55,
        f"Plan for: {user_name}",
        f"Date: {today}",
        "="*55,"",
    ]
    for ln in clean.split("\n"):
        ln = re.sub(r"\*\*(.+?)\*\*",r"\1",ln)
        ln = re.sub(r"\*(.+?)\*",r"\1",ln)
        ln = ln.replace("##","").replace("#","").strip()
        lines.append(ln)
    lines += ["","="*55,"DISCLAIMER: For personalised medical nutrition, consult Dr. Meghana Kumare.",
              "Fortune Crest, Opp. Khare Town Post Office, Dharampeth, Nagpur - 440010"]
    buf = io.BytesIO()
    def w(s):
        if isinstance(s,str): s=s.encode("latin-1",errors="replace")
        buf.write(s)
    offs=[]; 
    def obj(n,c):
        offs.append((n,buf.tell())); w(f"{n} 0 obj\n")
        buf.write(c if isinstance(c,bytes) else c.encode("latin-1",errors="replace"))
        w("\nendobj\n")
    w("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    obj(1,"<< /Type /Catalog /Pages 2 0 R >>")
    obj(2,"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    pl=["BT","/F1 12 Tf","1 0 0 rg","50 800 Td","(Red Apple Wellness Diet Center) Tj",
        "0 0 0 rg","/F1 9 Tf","0 -14 Td","(RAW-DIET | SINCE 2008) Tj","0 -12 Td",
        "(+91 7774944783 | rawdiets12@gmail.com | raw-diet.com) Tj","0 -10 Td",
        "0.8 0.8 0.8 rg","(-----------------------------------------------------) Tj","0 0 0 rg",
        "0 -13 Td","/F1 10 Tf",
        f"(Plan for: {user_name.encode('latin-1','replace').decode('latin-1')}) Tj","0 -13 Td",
        f"(Date: {today}) Tj","0 -13 Td",
        "0.8 0.8 0.8 rg","(-----------------------------------------------------) Tj","0 0 0 rg",
        "0 -14 Td","/F1 9 Tf"]
    for ln in lines[8:55]:
        sl=ln.encode("latin-1","replace").decode("latin-1")
        sl=sl.replace("\\","\\\\").replace("(","\\(").replace(")","\\)")
        pl.append(f"({sl}) Tj"); pl.append("0 -13 Td")
    pl.append("ET")
    cb="\n".join(pl).encode("latin-1",errors="replace")
    obj(3,("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
           "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"))
    obj(4,f"<< /Length {len(cb)} >>\nstream\n".encode()+cb+b"\nendstream")
    obj(5,("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
           "/Encoding /WinAnsiEncoding >>"))
    xp=buf.tell(); w("xref\n"); w(f"0 {len(offs)+1}\n"); w("0000000000 65535 f \n")
    for _,o in sorted(offs): w(f"{o:010d} 00000 n \n")
    w("trailer\n"); w(f"<< /Size {len(offs)+1} /Root 1 0 R >>\n")
    w("startxref\n"); w(f"{xp}\n"); w("%%EOF\n")
    return buf.getvalue()

def is_pdf_requested(answer: str) -> bool:
    """Check if Gemini flagged a PDF download request in its response."""
    return "[PDF_REQUESTED]" in answer

def get_recent_messages(db, session_id: str, limit: int = 10) -> list:
    try:
        from models import Chat
        chats = (
            db.query(Chat)
            .filter(Chat.session_id == session_id)
            .order_by(Chat.created_at.desc())
            .limit(limit)
            .all()
        )
        return list(reversed(chats))
    except Exception as e:
        logger.error(f"❌ get_recent_messages error: {e}")
        return []
