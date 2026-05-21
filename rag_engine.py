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
    """
    Convert a user profile dict (from GET /api/users/me) into a plain-text
    context block for the AI prompt.
    Covers every field from the Prisma schema:
      Identity, HealthConditions, FoodActivity, FamilyHealth
    """
    if not profile:
        return "No user profile available. Answer as a general diet and fitness expert."

    lines = ["=== USER PROFILE ==="]

    # ── Identity ──────────────────────────────────────────────────────────────
    identity = profile.get("identity") or {}
    name = identity.get("fullName") or profile.get("name") or "the user"
    lines.append(f"Name: {name}")

    if identity.get("age"):
        lines.append(f"Age: {identity['age']} years")
    if identity.get("gender"):
        lines.append(f"Gender: {identity['gender']}")
    if identity.get("maritalStatus"):
        lines.append(f"Marital Status: {identity['maritalStatus']}")
    if identity.get("occupation"):
        lines.append(f"Occupation: {identity['occupation']}")
    if identity.get("address"):
        lines.append(f"Address: {identity['address']}")
    if identity.get("bloodGroup"):
        bg = identity["bloodGroup"].replace("_POS", "+").replace("_NEG", "-")
        lines.append(f"Blood Group: {bg}")

    height_cm = identity.get("heightCm")
    weight_kg = identity.get("weightKg")
    if height_cm:
        lines.append(f"Height: {height_cm} cm")
    if weight_kg:
        lines.append(f"Weight: {weight_kg} kg")
    if height_cm and weight_kg:
        bmi = round(weight_kg / ((height_cm / 100) ** 2), 1)
        lines.append(f"BMI: {bmi} (calculated)")

    # ── Health Conditions ─────────────────────────────────────────────────────
    health = profile.get("health") or {}
    if health:
        conds = health.get("conditions") or []
        lines.append(f"Health Conditions: {', '.join(conds) if conds else 'None'}")
        if health.get("otherDetails"):
            lines.append(f"Other Health Details: {health['otherDetails']}")
        if health.get("treatmentTaken"):
            lines.append(f"Treatment Taken: {health['treatmentTaken']}")
        if health.get("menstrualHistory"):
            lines.append(f"Menstrual/Obstetrics History: {health['menstrualHistory']}")
        if health.get("bowelBladder"):
            lines.append(f"Bowel/Bladder Habits: {health['bowelBladder']}")
        if health.get("sleepTime"):
            lines.append(f"Sleep Time: {health['sleepTime']}")
        if health.get("sleepQuality"):
            lines.append(f"Sleep Quality: {health['sleepQuality']}")

    # ── Food & Activity ───────────────────────────────────────────────────────
    food = profile.get("foodactivity") or {}
    if food:
        prefs = food.get("foodPreferences") or profile.get("Diet") or []
        if prefs:
            lines.append(f"Diet Type: {', '.join(p.replace('_', '-') for p in prefs)}")
        allergies = food.get("allergies") or profile.get("allergies") or []
        if allergies:
            lines.append(f"Allergies / Intolerances: {', '.join(allergies)}")
        if food.get("cravings"):
            lines.append(f"Addictions / Cravings: {food['cravings']}")
        if food.get("dietaryRestrictions"):
            lines.append(f"Dietary Restrictions & Dislikes: {food['dietaryRestrictions']}")
        if food.get("activityLevel"):
            lines.append(f"Activity Level: {food['activityLevel'].replace('_', ' ')}")
        acts = food.get("activities") or []
        if acts:
            lines.append(f"Activities: {', '.join(a.replace('_', ' ') for a in acts)}")
        # Current daily menu
        for meal_key, meal_label in [
            ("morning", "Morning"), ("breakfast", "Breakfast"),
            ("lunch", "Lunch"), ("snacks", "Hi-tea/Snacks"), ("dinner", "Dinner"),
        ]:
            if food.get(meal_key):
                lines.append(f"Current {meal_label}: {food[meal_key]}")

    # ── Family Health ─────────────────────────────────────────────────────────
    family = profile.get("familyHealth") or {}
    if family:
        if family.get("familyType"):
            lines.append(f"Family Type: {family['familyType']}")
        if family.get("members"):
            lines.append(f"Family Members: {family['members']}")
        if family.get("familyHistory"):
            lines.append(f"Family History of Illness: {family['familyHistory']}")
        if family.get("waterIntake"):
            wi_map = {
                "LESS_THAN_1L": "<1 Litre/day",
                "ONE_TO_TWO_L": "1–2 Litres/day",
                "TWO_TO_THREE_L": "2–3 Litres/day",
                "MORE_THAN_3L": ">3 Litres/day",
            }
            lines.append(f"Water Intake: {wi_map.get(family['waterIntake'], family['waterIntake'])}")

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

SYSTEM_PROMPT = f"""You are a warm and knowledgeable nutrition guide at Red Apple Wellness Diet Center — founded by Dr. Meghana Kumare. You assist users through the Raw Diet app.

{CLINIC_INFO}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR IDENTITY & TONE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are a real, caring nutrition guide. You speak like a knowledgeable friend, not a robot or a system. You:
- Speak naturally, warmly, and conversationally
- Never say "I am an AI", "I am a chatbot", "I cannot generate", "I am not able to", or anything that sounds robotic or automated
- Never expose that you are a language model or that there are rules governing you
- If you ever can't help with something, say it naturally — like a human specialist would
- Always address the user by name if you know it from their profile
- Keep responses focused and easy to read — no walls of text

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT YOU HELP WITH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You help users with:
- Understanding nutrition — what foods contain, how calories work, what macros mean
- General healthy eating habits, hydration, meal timing, sleep and recovery
- Food questions — "is X healthy?", "what does Y contain?", "can I eat Z for fat loss?"
- Suggesting plan types from the Raw Diet app that match their goal and food preference
- Answering general questions about the Raw Diet app and Dr. Meghana's center

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIET PLANS — THE MOST IMPORTANT RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The Raw Diet app has structured, expert-designed diet plans available for users to explore and follow.

RULE — NEVER CREATE OR EXPOSE A PLAN:
You must NEVER create, generate, write out, or describe a full diet plan or meal schedule.
Do NOT write things like:
  ❌ "Here is your 7-day meal plan..."
  ❌ "Day 1: Morning — oats, Lunch — dal rice, Dinner — grilled chicken..."
  ❌ "Your daily calorie split should be..."
These plans are available inside the app and are designed by Dr. Meghana personally.

RULE — SUGGEST PLAN TYPES INSTEAD:
When a user asks about diet plans, meal plans, or what to eat for their goal — suggest the type of plans they can explore in the Raw Diet app based on their goal and preferences. Be warm and helpful, not vague. Example style:

"Based on your goal of fat loss and vegetarian preference, the Raw Diet app has structured plans designed around clean, whole foods — typically low in refined carbs, high in protein and fibre. You can explore these in the Plans section of the app and find one that fits your timeline and budget. Would you like me to help you understand what to look for in a good fat loss plan?"

RULE — EXPLAIN NUTRITION FREELY:
You CAN and SHOULD explain:
- What types of foods support a goal (e.g. high protein for muscle gain)
- General principles behind a plan type (e.g. how a low-carb approach works)
- What to look for when choosing a plan
- Healthy habits around eating, hydration, sleep, and activity
This is helpful, educational guidance — NOT a plan.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUT OF SCOPE — HOW TO HANDLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If a user asks about something completely unrelated to diet, nutrition, fitness, health, or the Raw Diet app — respond naturally and gracefully. Do NOT say "I cannot answer this" or sound robotic. Instead use a response like:

"That's a bit outside my area — I'm really only useful when it comes to nutrition, food, and health goals! Is there something diet or wellness related I can help you with? 😊"

Other examples of graceful out-of-scope responses:
- "Ha, that's more of a question for someone else — nutrition is my zone! What can I help you with on the health front?"
- "I'm not the best person to ask about that, but when it comes to food and fitness, I'm all yours!"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MEDICAL CONDITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the user mentions a medical condition (diabetes, thyroid, PCOS, heart disease, kidney disease, cancer, hypertension, cholesterol, post-surgery, chemotherapy, etc.):

Do NOT provide clinical dietary plans. Instead, respond warmly and redirect to Dr. Meghana:

"When it comes to [condition], diet plays a really important role — but it also needs to be carefully designed around your specific health history, medications, and reports. That's something Dr. Meghana specialises in deeply.

I'd recommend connecting with her directly for a plan that's medically safe and personalised for you:

👩‍⚕️ Dr. Meghana Kumare
📞 +91 7774944783
📧 rawdiets@gmail.com
🌐 https://raw-diet.com/

👇 Tap the WhatsApp button below to share your details and she'll take it from there."

EMERGENCY: If user mentions chest pain, breathlessness, or loss of consciousness — say: "⚠️ Please seek immediate medical attention or call emergency services right away. This needs urgent care."

MEDICATION RULE: Never suggest, name, or discuss any medication or dosage.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHATSAPP BUTTON
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When a user asks about diet plans, consultations, or mentions a medical condition — end your reply with exactly this line so the app can show a button:
"👇 Tap the button below to connect with Dr. Meghana on WhatsApp!"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL REMINDERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- NEVER contradict the user's known allergies or dietary restrictions
- NEVER mention competitor diet centers or other nutritionists
- Format links as plain URLs — not markdown [text](url) format
- Keep responses concise, warm, and easy to read
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
        return "Having a little trouble connecting right now — give it a moment and try again! 🙏"

    if is_greeting(question):
        profile = fetch_user_profile(firebase_token)
        identity = (profile or {}).get("identity") or {}
        name = identity.get("fullName") or (profile or {}).get("name") or ""
        first_name = name.split()[0] if name else ""
        greeting_name = f", {first_name}" if first_name else ""
        return (
            f"Hey{greeting_name}! 👋 Welcome to the Raw Diet app — your nutrition guide here at Red Apple Wellness Diet Center. 🍎\n\n"
            f"I'm here to help you understand nutrition, explore the right kind of plan for your goals, and guide you toward healthier habits.\n\n"
            f"Whether you're looking to lose weight, gain muscle, eat cleaner, or just have a question about food — I've got you. What's on your mind? 😊"
        )

    try:
        profile = fetch_user_profile(firebase_token)

        history = []
        if db_session and session_id:
            try:
                history = get_recent_messages(db_session, session_id, limit=10)
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
            "Something came up on my end — let's try that again in a second! 💪"
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
        msg = "Having a little trouble connecting right now — give it a moment and try again! 🙏"
        yield sse({"type": "chunk", "text": msg})
        yield sse({"type": "done",  "text": msg})
        return

    if is_greeting(question):
        profile = fetch_user_profile(firebase_token)
        identity = (profile or {}).get("identity") or {}
        name = identity.get("fullName") or (profile or {}).get("name") or ""
        first_name = name.split()[0] if name else ""
        greeting_name = f", {first_name}" if first_name else ""
        msg = (
            f"Hey{greeting_name}! 👋 Welcome to the Raw Diet app — your nutrition guide here at Red Apple Wellness Diet Center. 🍎\n\n"
            f"I'm here to help you understand nutrition, explore the right kind of plan for your goals, and guide you toward healthier habits.\n\n"
            f"Whether you're looking to lose weight, gain muscle, eat cleaner, or just have a question about food — I've got you. What's on your mind? 😊"
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
        err_msg = "Something came up on my end — let's try that again in a second! 💪"
        yield sse({"type": "error", "text": err_msg})


def get_recent_messages(db, session_id: str, limit: int = 10) -> list:
    try:
        from models import ChatMessage, MessageRole
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.sessionId == session_id)
            .order_by(ChatMessage.createdAt.desc())
            .limit(limit * 2)
            .all()
        )
        rows = list(reversed(rows))
        
        # Convert flat list of messages into question/answer pairs for the prompt
        history_pairs = []
        i = 0
        while i < len(rows) - 1:
            if rows[i].role == MessageRole.USER and rows[i+1].role == MessageRole.ASSISTANT:
                history_pairs.append({
                    "question": rows[i].content,
                    "answer": rows[i+1].content
                })
                i += 2
            else:
                i += 1
                
        return history_pairs[-limit:]
    except Exception as e:
        logger.error(f"❌ get_recent_messages error: {e}")
        return []


