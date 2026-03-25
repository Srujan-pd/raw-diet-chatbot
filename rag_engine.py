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
import json
from typing import Generator, Optional

from google import genai
from dotenv import load_dotenv

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
  Email   : rawdiets@gmail.com
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

LINK FORMATTING — IMPORTANT:
- Always format website links as plain clickable URLs, e.g.: https://raw-diet.com/
- Always format email addresses as plain text, e.g.: rawdiets@gmail.com
- Always format phone numbers as plain text, e.g.: +91 7774944783
- Do NOT use markdown link syntax like [text](url) — use the raw URL directly so it is clickable in any chat interface

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

👩‍⚕️ Dr. Meghana Kumare
Dietician & Sports Nutritionist | 20+ years experience
Red Apple Wellness Diet Center

📞 +91 7774944783
📧 meghana17kumare@gmail.com
📍 Fortune Crest, Opp. Khare Town Post Office, Dharampeth, Nagpur – 440010
🌐 https://raw-diet.com/

Dr. Meghana specialises in clinical nutrition for [condition] and will create a plan tailored to your specific needs and medications."

EMERGENCY RULE: If user reports chest pain, difficulty breathing, severe dizziness, or loss of consciousness — say: "⚠️ Please call emergency services or go to the nearest hospital immediately. This needs urgent medical attention."

MEDICATION RULE: NEVER name, suggest, or discuss any medication or dosage under any circumstances.

══════════════════════════════════════════════════════

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

    history_block = ""
    if chat_history:
        turns = []
        for msg in chat_history[-10:]:
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
