"""
rag_engine.py — AI answer engine for Raw Diet Personal Trainer chatbot.

Context sources (all from DB):
  1. User profile: Identity, HealthConditions, FoodActivity, FamilyHealth
  2. Active plan: UserDietPlan → DietPlan → DietDay → Meal → MealRecipe → Recipe
                  UserRationPlan → RationPlan → RationDay → RationMeal → RationMealItem
  3. Available plans (for free users): DietPlan + RationPlan (ACTIVE, isGlobal)
  4. Conversation history: ChatMessage
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
GEMINI_MODEL  = "gemini-2.5-flash"


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


# ── Raw Diet backend API (fallback only) ──────────────────────────────────────

RAW_DIET_API_BASE = os.getenv(
    "RAW_DIET_API_BASE",
    "https://test---raw-diet-backend-5rnsarrnya-uc.a.run.app"
)


def fetch_user_profile(firebase_token: Optional[str]) -> Optional[dict]:
    """Fallback: fetch profile from backend API using Firebase JWT."""
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


from sqlalchemy import text as sql_text


# ── DB profile fetch (primary) ────────────────────────────────────────────────

def fetch_user_profile_db(firebase_uid: Optional[str], db) -> Optional[dict]:
    """
    Fetch full user context from DB:
      - Identity, HealthConditions, FoodActivity, FamilyHealth
      - Active DietPlan (with today's meals + recipes)
      - Active RationPlan (with today's meals + items)
      - Available global plans (for free users)
    """
    if not firebase_uid or not db:
        return None
    try:
        # ── User row ──────────────────────────────────────────────────────────
        user_row = db.execute(
            sql_text('SELECT id, email, "isOnboarded" FROM "User" WHERE "firebaseUid" = :uid LIMIT 1'),
            {"uid": firebase_uid}
        ).fetchone()
        if not user_row:
            return None

        user_id = user_row[0]
        profile = {"id": user_id, "email": user_row[1], "isOnboarded": user_row[2]}

        # ── Identity ──────────────────────────────────────────────────────────
        irow = db.execute(sql_text("""
            SELECT "fullName", age, gender, "heightCm", "weightKg",
                   "bloodGroup", "maritalStatus", occupation, address, contact
            FROM "Identity" WHERE "userId" = :uid LIMIT 1
        """), {"uid": user_id}).fetchone()
        if irow:
            profile["identity"] = {
                "fullName": irow[0], "age": irow[1], "gender": irow[2],
                "heightCm": irow[3], "weightKg": irow[4], "bloodGroup": irow[5],
                "maritalStatus": irow[6], "occupation": irow[7],
                "address": irow[8], "contact": irow[9],
            }

        # ── HealthConditions ──────────────────────────────────────────────────
        hrow = db.execute(sql_text("""
            SELECT conditions, "otherDetails", "treatmentTaken",
                   "menstrualHistory", "bowelBladder", "sleepTime", "sleepQuality"
            FROM "HealthConditions" WHERE "userId" = :uid LIMIT 1
        """), {"uid": user_id}).fetchone()
        if hrow:
            profile["health"] = {
                "conditions": hrow[0] or [], "otherDetails": hrow[1],
                "treatmentTaken": hrow[2], "menstrualHistory": hrow[3],
                "bowelBladder": hrow[4], "sleepTime": hrow[5], "sleepQuality": hrow[6],
            }

        # ── FoodActivity ──────────────────────────────────────────────────────
        frow = db.execute(sql_text("""
            SELECT "foodPreferences", allergies, cravings, "dietaryRestrictions",
                   "activityLevel", activities, morning, breakfast, lunch, snacks, dinner
            FROM "FoodActivity" WHERE "userId" = :uid LIMIT 1
        """), {"uid": user_id}).fetchone()
        if frow:
            profile["foodactivity"] = {
                "foodPreferences": frow[0] or [], "allergies": frow[1] or [],
                "cravings": frow[2], "dietaryRestrictions": frow[3],
                "activityLevel": frow[4], "activities": frow[5] or [],
                "morning": frow[6], "breakfast": frow[7],
                "lunch": frow[8], "snacks": frow[9], "dinner": frow[10],
            }

        # ── FamilyHealth ──────────────────────────────────────────────────────
        farow = db.execute(sql_text("""
            SELECT "familyType", members, "familyHistory", "waterIntake"
            FROM "FamilyHealth" WHERE "userId" = :uid LIMIT 1
        """), {"uid": user_id}).fetchone()
        if farow:
            profile["familyHealth"] = {
                "familyType": farow[0], "members": farow[1],
                "familyHistory": farow[2], "waterIntake": farow[3],
            }

        # ── Active DietPlan ───────────────────────────────────────────────────
        # UserDietPlan → DietPlan
        udp_row = db.execute(sql_text("""
            SELECT udp.id, udp."planId", udp."currentDay", udp."startDate",
                   dp.name, dp.description, dp."dietType", dp.calories,
                   dp.protein, dp.duration
            FROM "UserDietPlan" udp
            JOIN "DietPlan" dp ON dp.id = udp."planId"
            WHERE udp."userId" = :uid AND udp."isActive" = true
            LIMIT 1
        """), {"uid": user_id}).fetchone()

        if udp_row:
            current_day = udp_row[2] or 1
            profile["activeDietPlan"] = {
                "userPlanId":   udp_row[0],
                "planId":       udp_row[1],
                "currentDay":   current_day,
                "startDate":    str(udp_row[3]),
                "name":         udp_row[4],
                "description":  udp_row[5],
                "dietType":     udp_row[6],
                "calories":     udp_row[7],
                "protein":      udp_row[8],
                "duration":     udp_row[9],
                "todayMeals":   [],
            }

            # Fetch today's DietDay meals + recipes
            day_row = db.execute(sql_text("""
                SELECT dd.id FROM "DietDay" dd
                WHERE dd."planId" = :pid AND dd."dayNumber" = :day
                LIMIT 1
            """), {"pid": udp_row[1], "day": current_day}).fetchone()

            if day_row:
                meals = db.execute(sql_text("""
                    SELECT m.id, m.type FROM "Meal" m
                    WHERE m."dayId" = :did
                    ORDER BY m.type
                """), {"did": day_row[0]}).fetchall()

                today_meals = []
                for meal in meals:
                    recipes = db.execute(sql_text("""
                        SELECT r.name, r.calories, r."prepTimeMin",
                               r."proteinG", r."carbsG", r."fatG",
                               r.description
                        FROM "MealRecipe" mr
                        JOIN "Recipe" r ON r.id = mr."recipeId"
                        WHERE mr."mealId" = :mid
                        ORDER BY mr."isPrimary" DESC
                    """), {"mid": meal[0]}).fetchall()

                    today_meals.append({
                        "mealType": meal[1],
                        "recipes": [
                            {
                                "name":        r[0],
                                "calories":    r[1],
                                "prepTimeMin": r[2],
                                "proteinG":    r[3],
                                "carbsG":      r[4],
                                "fatG":        r[5],
                                "description": r[6],
                            }
                            for r in recipes
                        ],
                    })
                profile["activeDietPlan"]["todayMeals"] = today_meals

        # ── Active RationPlan ─────────────────────────────────────────────────
        # UserRationPlan → RationPlan
        urp_row = db.execute(sql_text("""
            SELECT urp.id, urp."planId", urp."startDate",
                   rp.name, rp.description, rp."dietType", rp.duration,
                   rp."specialComments", rp."waterIntake", rp."oilLimit",
                   rp."gheeLimit", rp."allowedDrinks"
            FROM "UserRationPlan" urp
            JOIN "RationPlan" rp ON rp.id = urp."planId"
            WHERE urp."userId" = :uid AND urp."isActive" = true
            LIMIT 1
        """), {"uid": user_id}).fetchone()

        if urp_row:
            # Calculate current day from startDate
            from datetime import datetime, timezone
            start = urp_row[2]
            if hasattr(start, 'replace'):
                start = start.replace(tzinfo=None)
            current_day_r = max(1, (datetime.utcnow() - start).days + 1) if start else 1

            profile["activeRationPlan"] = {
                "userPlanId":      urp_row[0],
                "planId":          urp_row[1],
                "startDate":       str(urp_row[2]),
                "currentDay":      current_day_r,
                "name":            urp_row[3],
                "description":     urp_row[4],
                "dietType":        urp_row[5],
                "duration":        urp_row[6],
                "specialComments": urp_row[7],
                "waterIntake":     urp_row[8],
                "oilLimit":        urp_row[9],
                "gheeLimit":       urp_row[10],
                "allowedDrinks":   urp_row[11] or [],
                "todayMeals":      [],
            }

            # Fetch today's RationDay meals + items
            rday_row = db.execute(sql_text("""
                SELECT rd.id FROM "RationDay" rd
                WHERE rd."planId" = :pid AND rd."dayNumber" = :day
                LIMIT 1
            """), {"pid": urp_row[1], "day": current_day_r}).fetchone()

            if rday_row:
                rmeals = db.execute(sql_text("""
                    SELECT rm.id, rm.type, rm.title, rm.time
                    FROM "RationMeal" rm
                    WHERE rm."dayId" = :did
                    ORDER BY rm."sortOrder", rm.type
                """), {"did": rday_row[0]}).fetchall()

                today_rmeals = []
                for rmeal in rmeals:
                    items = db.execute(sql_text("""
                        SELECT rmi.name, rmi.quantity, rmi.unit,
                               rmi.notes, rmi.instruction, rmi.category
                        FROM "RationMealItem" rmi
                        WHERE rmi."mealId" = :mid
                        ORDER BY rmi."createdAt"
                    """), {"mid": rmeal[0]}).fetchall()

                    today_rmeals.append({
                        "mealType": rmeal[1],
                        "title":    rmeal[2],
                        "time":     rmeal[3],
                        "items": [
                            {
                                "name":        item[0],
                                "quantity":    item[1],
                                "unit":        item[2],
                                "notes":       item[3],
                                "instruction": item[4],
                                "category":    item[5],
                            }
                            for item in items
                        ],
                    })
                profile["activeRationPlan"]["todayMeals"] = today_rmeals

        # ── Available global plans (for free / non-active users) ──────────────
        if not profile.get("activeDietPlan") and not profile.get("activeRationPlan"):
            dp_rows = db.execute(sql_text("""
                SELECT id, name, description, "dietType", duration, calories
                FROM "DietPlan"
                WHERE status = 'ACTIVE' AND "isGlobal" = true
                ORDER BY "createdAt" DESC
                LIMIT 10
            """)).fetchall()

            rp_rows = db.execute(sql_text("""
                SELECT id, name, description, "dietType", duration
                FROM "RationPlan"
                WHERE status = 'ACTIVE' AND "isGlobal" = true
                ORDER BY "createdAt" DESC
                LIMIT 10
            """)).fetchall()

            if dp_rows or rp_rows:
                profile["availablePlans"] = {
                    "dietPlans": [
                        {
                            "id": r[0], "name": r[1], "description": r[2],
                            "dietType": r[3], "duration": r[4], "calories": r[5],
                        }
                        for r in dp_rows
                    ],
                    "rationPlans": [
                        {
                            "id": r[0], "name": r[1], "description": r[2],
                            "dietType": r[3], "duration": r[4],
                        }
                        for r in rp_rows
                    ],
                }

        logger.info(f"✅ DB profile fetched for user {user_id}")
        return profile

    except Exception as e:
        logger.warning(f"⚠️ fetch_user_profile_db failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None


# ── Plan status helpers ────────────────────────────────────────────────────────

def get_user_plan_status(profile) -> str:
    """'paid' if user has an active DietPlan or RationPlan, else 'free'."""
    if not profile:
        return 'free'
    if profile.get("activeDietPlan") or profile.get("activeRationPlan"):
        return 'paid'
    return 'free'


def get_active_plan_summary(profile) -> Optional[dict]:
    """Return whichever active plan exists (DietPlan takes priority)."""
    return profile.get("activeDietPlan") or profile.get("activeRationPlan")


# ── Context builders ──────────────────────────────────────────────────────────

def build_user_context(profile: Optional[dict]) -> str:
    if not profile:
        return "No user profile available. Answer as a general diet and fitness expert."

    lines = ["=== USER PROFILE ==="]

    identity = profile.get("identity") or {}
    name = identity.get("fullName") or profile.get("name") or "the user"
    lines.append(f"Name: {name}")
    if identity.get("age"):       lines.append(f"Age: {identity['age']} years")
    if identity.get("gender"):    lines.append(f"Gender: {identity['gender']}")
    if identity.get("heightCm"):  lines.append(f"Height: {identity['heightCm']} cm")
    if identity.get("weightKg"):  lines.append(f"Weight: {identity['weightKg']} kg")
    h, w = identity.get("heightCm"), identity.get("weightKg")
    if h and w:
        lines.append(f"BMI: {round(w / ((h / 100) ** 2), 1)} (calculated)")
    if identity.get("bloodGroup"):
        bg = identity["bloodGroup"].replace("_POS", "+").replace("_NEG", "-")
        lines.append(f"Blood Group: {bg}")
    if identity.get("occupation"):    lines.append(f"Occupation: {identity['occupation']}")
    if identity.get("maritalStatus"): lines.append(f"Marital Status: {identity['maritalStatus']}")

    health = profile.get("health") or {}
    if health:
        conds = health.get("conditions") or []
        lines.append(f"Health Conditions: {', '.join(conds) if conds else 'None'}")
        if health.get("sleepQuality"): lines.append(f"Sleep Quality: {health['sleepQuality']}")
        if health.get("sleepTime"):    lines.append(f"Sleep Time: {health['sleepTime']}")

    food = profile.get("foodactivity") or {}
    if food:
        prefs = food.get("foodPreferences") or []
        if prefs: lines.append(f"Diet Type: {', '.join(p.replace('_', '-') for p in prefs)}")
        allergies = food.get("allergies") or []
        if allergies: lines.append(f"Allergies: {', '.join(allergies)}")
        if food.get("activityLevel"):
            lines.append(f"Activity Level: {food['activityLevel'].replace('_', ' ')}")
        acts = food.get("activities") or []
        if acts: lines.append(f"Activities: {', '.join(a.replace('_', ' ') for a in acts)}")
        if food.get("cravings"):            lines.append(f"Cravings: {food['cravings']}")
        if food.get("dietaryRestrictions"): lines.append(f"Dietary Restrictions: {food['dietaryRestrictions']}")

    family = profile.get("familyHealth") or {}
    if family:
        if family.get("waterIntake"):
            wi_map = {
                "LESS_THAN_1L": "<1 L/day", "ONE_TO_TWO_L": "1–2 L/day",
                "TWO_TO_THREE_L": "2–3 L/day", "MORE_THAN_3L": ">3 L/day",
            }
            lines.append(f"Water Intake: {wi_map.get(family['waterIntake'], family['waterIntake'])}")

    lines.append("===================")
    return "\n".join(lines)


def build_active_plan_context(profile: Optional[dict]) -> str:
    """Build today's meal context from whichever active plan the user has."""
    if not profile:
        return ""

    lines = []

    # ── DietPlan (Recipe-based) ───────────────────────────────────────────────
    adp = profile.get("activeDietPlan")
    if adp:
        lines.append("\n=== YOUR ACTIVE DIET PLAN ===")
        lines.append(f"Plan Name  : {adp.get('name', 'Your Plan')}")
        if adp.get("description"): lines.append(f"Description: {adp['description']}")
        if adp.get("dietType"):    lines.append(f"Diet Type  : {adp['dietType']}")
        if adp.get("calories"):    lines.append(f"Target Cal : {adp['calories']} kcal/day")
        if adp.get("protein"):     lines.append(f"Target Prot: {adp['protein']} g/day")
        lines.append(f"Day        : {adp.get('currentDay', 1)} of {adp.get('duration', '?')}")

        today_meals = adp.get("todayMeals") or []
        if today_meals:
            lines.append(f"\nToday's Meals (Day {adp.get('currentDay', 1)}):")
            for meal in today_meals:
                lines.append(f"\n  [{meal['mealType']}]")
                for r in meal.get("recipes") or []:
                    cal  = f" • {r['calories']} kcal" if r.get("calories") else ""
                    prot = f" • {r['proteinG']}g protein" if r.get("proteinG") else ""
                    lines.append(f"    - {r['name']}{cal}{prot}")
                    if r.get("description"):
                        lines.append(f"      ({r['description']})")
        else:
            lines.append("  (No meal data for today yet)")
        lines.append("==============================")

    # ── RationPlan (Item-based) ───────────────────────────────────────────────
    arp = profile.get("activeRationPlan")
    if arp:
        lines.append("\n=== YOUR ACTIVE RATION PLAN ===")
        lines.append(f"Plan Name: {arp.get('name', 'Your Plan')}")
        if arp.get("description"):     lines.append(f"Description: {arp['description']}")
        if arp.get("specialComments"): lines.append(f"Notes: {arp['specialComments']}")
        if arp.get("waterIntake"):     lines.append(f"Water Target: {arp['waterIntake']}")
        if arp.get("oilLimit"):        lines.append(f"Oil Limit: {arp['oilLimit']}")
        if arp.get("gheeLimit"):       lines.append(f"Ghee Limit: {arp['gheeLimit']}")
        drinks = arp.get("allowedDrinks") or []
        if drinks: lines.append(f"Allowed Drinks: {', '.join(drinks)}")
        lines.append(f"Day: {arp.get('currentDay', 1)} of {arp.get('duration', '?')}")

        today_rmeals = arp.get("todayMeals") or []
        if today_rmeals:
            lines.append(f"\nToday's Meals (Day {arp.get('currentDay', 1)}):")
            for meal in today_rmeals:
                title = f" – {meal['title']}" if meal.get("title") else ""
                time  = f" ({meal['time']})" if meal.get("time") else ""
                lines.append(f"\n  [{meal['mealType']}]{title}{time}")
                for item in meal.get("items") or []:
                    qty  = f" {item['quantity']}" if item.get("quantity") else ""
                    unit = f" {item['unit']}" if item.get("unit") else ""
                    note = f" — {item['notes']}" if item.get("notes") else ""
                    inst = f" ({item['instruction']})" if item.get("instruction") else ""
                    lines.append(f"    - {item['name']}{qty}{unit}{note}{inst}")
        else:
            lines.append("  (No meal data for today yet)")
        lines.append("================================")

    return "\n".join(lines)


def build_available_plans_context(profile: Optional[dict]) -> str:
    """
    List available global plans from DB.
    For free users: shown always (they have no active plan).
    For paid users: shown when they ask about other plans.
    Always filter to match user's food preferences when possible.
    """
    avail = (profile or {}).get("availablePlans") or {}
    dp    = avail.get("dietPlans") or []
    rp    = avail.get("rationPlans") or []

    # Also show for paid users if they have availablePlans populated
    # (fetch_user_profile_db only populates availablePlans when no active plan exists,
    #  so for paid users we skip this block — handled by active plan context instead)
    if not dp and not rp:
        return ""

    # Get user food preferences to annotate matches
    food  = (profile or {}).get("foodactivity") or {}
    prefs = [p.lower() for p in (food.get("foodPreferences") or [])]

    lines = ["\n=== PLANS AVAILABLE IN THE RAW DIET APP ==="]
    if dp:
        lines.append("\nDiet Plans (Recipe-based):")
        for p in dp:
            diet = (p.get("dietType") or "").lower()
            match = " ✓ matches your diet" if diet and diet in prefs else ""
            lines.append(f"  • {p.get('name', 'Unnamed')}{match}")
            if p.get("dietType"):    lines.append(f"    Diet: {p['dietType']}")
            if p.get("duration"):    lines.append(f"    Duration: {p['duration']} days")
            if p.get("calories"):    lines.append(f"    Calories: {p['calories']} kcal/day")
            if p.get("description"): lines.append(f"    Info: {p['description']}")
    if rp:
        lines.append("\nRation Plans (Portion-based):")
        for p in rp:
            diet = (p.get("dietType") or "").lower()
            match = " ✓ matches your diet" if diet and diet in prefs else ""
            lines.append(f"  • {p.get('name', 'Unnamed')}{match}")
            if p.get("dietType"):    lines.append(f"    Diet: {p['dietType']}")
            if p.get("duration"):    lines.append(f"    Duration: {p['duration']} days")
            if p.get("description"): lines.append(f"    Info: {p['description']}")
    lines.append("\nAlways refer to plans by their exact name above. Never invent plan names.")
    lines.append("===========================================")
    return "\n".join(lines)


# ── System prompt ──────────────────────────────────────────────────────────────

CLINIC_INFO = """
=== RED APPLE WELLNESS DIET CENTER ===
Website : https://raw-diet.com/
Founder : Dr. Meghana Kumare — Dietician & Sports Nutritionist | 20+ years
Contact : +91 7774944783 | rawdiets@gmail.com
Centers : Nagpur | Mumbai | Dubai
=======================================
"""

SYSTEM_PROMPT = f"""You are a warm nutrition guide at Red Apple Wellness Diet Center (Raw Diet app).

{CLINIC_INFO}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE LENGTH — HARD RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Maximum 3–4 sentences per response
- NEVER start with "Hey [Name]!" go straight to the answer
- No long bullet lists (max 3 bullets if truly needed, one line each)
- No repeating yourself
- Be warm but brief like a quick helpful text from a friend

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLAN-AWARE BEHAVIOUR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PAID USER (has active plan):
- When they ask about meals, snacks, or what to eat → suggest ONLY from their plan's today meals
- Reference the actual recipe/item names from the plan context provided
- Never suggest foods outside their plan
- If they ask about hunger/snack → check if SNACK meal exists in today's plan, suggest that item
- If no snack in today's plan → "Your plan doesn't have a listed snack for today try sipping water first and check the Plans tab for guidance."

FREE USER (no active plan):
- Never suggest specific snacks, meals, or recipes
- When asked what to eat or snack ideas → redirect to Plans tab
- "To get meal and snack suggestions tailored to your goal, check out the Plans section in the app that's where the expert-designed options are."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIET PLANS — RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- NEVER create or write out a full meal plan or diet chart
- For plan questions → reference available plan names from the context, direct to Plans tab
- Free users: "Explore the Plans section in the app for plans designed by Dr. Meghana."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUT OF SCOPE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"That's outside my area — nutrition and health is my zone! What can I help you with? 😊"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MEDICAL CONDITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Redirect briefly: "For [condition], Dr. Meghana can personalise a safe plan for you reach her at +91 7774944783 or rawdiets@gmail.com"
EMERGENCY: "⚠️ Please seek immediate medical attention right away."
Never suggest medications.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHATSAPP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Never mention WhatsApp in your text. The app has a separate button for that.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL REMINDERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Never contradict the user's allergies or dietary restrictions
- Never mention competitor diet centers
- Format links as plain URLs — not markdown
- SHORT. WARM. PLAN-AWARE. Every time.
"""


# ── Prompt builder ─────────────────────────────────────────────────────────────

FREE_USER_ADDENDUM = """
USER STATUS: FREE (no active plan)
- Do NOT suggest specific foods, snacks, or recipes
- Guide them to the Plans tab for anything food-specific
- Answer general nutrition questions briefly
"""

PAID_USER_ADDENDUM = """
USER STATUS: PAID (has active plan)
- For food/meal/snack questions: reference ONLY today's meals from their plan above
- Use the actual recipe/item names provided in the plan context
- Do not suggest foods outside their plan
"""


def build_prompt(
    user_message: str,
    profile: Optional[dict],
    chat_history: list,
    goal_hint: Optional[str] = None,
) -> str:
    user_ctx         = build_user_context(profile)
    active_plan_ctx  = build_active_plan_context(profile)
    avail_plans_ctx  = build_available_plans_context(profile)
    goal_line        = f"\nUser's stated goal: {goal_hint}" if goal_hint else ""

    history_block = ""
    if chat_history:
        turns = []
        for msg in chat_history[-6:]:
            turns.append(f"User: {msg.get('question', '')}")
            turns.append(f"Trainer: {msg.get('answer', '')}")
        history_block = "\n=== RECENT CONVERSATION ===\n" + "\n".join(turns) + "\n===========================\n"

    plan_status   = get_user_plan_status(profile)
    plan_addendum = PAID_USER_ADDENDUM if plan_status == 'paid' else FREE_USER_ADDENDUM

    return f"""{SYSTEM_PROMPT}{plan_addendum}

{user_ctx}{active_plan_ctx}{avail_plans_ctx}{goal_line}
{history_block}
REMINDER: 3–4 sentences max. Do NOT open with "Hey [Name]!". Be direct and warm.

User message: {user_message}

Trainer response:"""


# ── Response length guard ──────────────────────────────────────────────────────

def _truncate_gemini_answer(text: str, max_sentences: int = 5) -> str:
    import re
    if not text:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sentences) <= max_sentences:
        return text
    trimmed = " ".join(sentences[:max_sentences])
    logger.info(f"✂️ Trimmed response from {len(sentences)} to {max_sentences} sentences")
    return trimmed


# ── Intent helpers ─────────────────────────────────────────────────────────────

CONSULT_KEYWORDS = [
    "consult", "consultation", "book appointment", "speak to doctor",
    "talk to meghana", "contact meghana", "speak to dietician",
    "book a session", "want to consult", "connect with doctor",
    "reach meghana", "contact the clinic", "i want to meet",
    "can i book", "how to consult", "take appointment", "get appointment",
]


def is_consult_intent(message: str) -> bool:
    msg = message.lower().strip()
    return any(kw in msg for kw in CONSULT_KEYWORDS)


def is_greeting(text: str) -> bool:
    greetings = {"hi", "hello", "hey", "good morning", "good afternoon",
                 "good evening", "hiya", "howdy", "namaste", "helo", "hii", "yo"}
    t = text.lower().strip().rstrip("!.,")
    return t in greetings or any(t.startswith(g + " ") for g in greetings)


HUNGER_KEYWORDS = [
    "i'm hungry", "im hungry", "i am hungry", "feeling hungry", "i feel hungry",
    "so hungry", "very hungry", "starving", "need a snack", "want a snack",
    "can i eat", "what can i eat", "what should i eat", "what to eat now",
    "i need to eat", "craving something", "need something to eat",
    "can i have a snack", "snack suggestion", "suggest a snack",
    "hungry right now", "need food",
]


def is_hunger_intent(message: str) -> bool:
    msg = message.lower().strip()
    return any(kw in msg for kw in HUNGER_KEYWORDS)


# Meal type display order for hunger response
_MEAL_ORDER = [
    "MORNING", "BREAKFAST", "MID_AFTERNOON", "LUNCH", "SNACK", "DINNER", "BED_TIME"
]

_MEAL_LABELS = {
    "MORNING":       "Morning",
    "BREAKFAST":     "Breakfast",
    "MID_AFTERNOON": "Mid-Afternoon",
    "LUNCH":         "Lunch",
    "SNACK":         "Snack",
    "DINNER":        "Dinner",
    "BED_TIME":      "Bed-Time",
}


def _build_plan_meals_text(profile: dict) -> str:
    """Flatten today's meals from whichever active plan into readable text for the prompt."""
    lines = []

    adp = profile.get("activeDietPlan")
    if adp:
        lines.append(f"Plan: {adp.get('name', 'Diet Plan')} (Day {adp.get('currentDay', 1)})")
        for meal in adp.get("todayMeals") or []:
            mtype = _MEAL_LABELS.get(meal.get("mealType", "").upper(), meal.get("mealType", ""))
            recipes = meal.get("recipes") or []
            if recipes:
                recipe_parts = []
                for r in recipes:
                    cal  = f" ({r['calories']} kcal)" if r.get("calories") else ""
                    prot = f", {r['proteinG']}g protein" if r.get("proteinG") else ""
                    recipe_parts.append(f"{r['name']}{cal}{prot}")
                lines.append(f"  {mtype}: {', '.join(recipe_parts)}")
        return "\n".join(lines)

    arp = profile.get("activeRationPlan")
    if arp:
        lines.append(f"Plan: {arp.get('name', 'Ration Plan')} (Day {arp.get('currentDay', 1)})")
        if arp.get("specialComments"):
            lines.append(f"Note: {arp['specialComments']}")
        for meal in arp.get("todayMeals") or []:
            mtype = _MEAL_LABELS.get(meal.get("mealType", "").upper(), meal.get("mealType", ""))
            items = meal.get("items") or []
            if items:
                item_parts = []
                for i in items:
                    qty  = f" {i['quantity']}" if i.get("quantity") else ""
                    unit = f" {i['unit']}"     if i.get("unit")     else ""
                    item_parts.append(f"{i['name']}{qty}{unit}".strip())
                time_note = f" ({meal['time']})" if meal.get("time") else ""
                lines.append(f"  {mtype}{time_note}: {', '.join(item_parts)}")
        return "\n".join(lines)

    return ""


def _build_available_plans_text(profile: dict) -> str:
    """Short summary of available plans for free user hunger prompt."""
    avail = profile.get("availablePlans") or {}
    dp    = avail.get("dietPlans") or []
    rp    = avail.get("rationPlans") or []
    lines = []
    for p in (dp + rp)[:5]:  # max 5 plans to keep prompt lean
        name = p.get("name", "Plan")
        dur  = f"{p['duration']} days" if p.get("duration") else ""
        diet = p.get("dietType", "")
        desc = p.get("description", "")
        parts = [x for x in [diet, dur, desc] if x]
        lines.append(f"- {name}: {' | '.join(parts)}" if parts else f"- {name}")
    return "\n".join(lines)


def _hunger_response_paid(profile: dict) -> str:
    """
    For paid users: build a focused Gemini prompt using their actual plan meals.
    Gemini suggests what to eat from within the plan not random food.
    """
    meals_text = _build_plan_meals_text(profile)

    if not meals_text:
        adp = profile.get("activeDietPlan") or profile.get("activeRationPlan") or {}
        return (
            f"You're on {adp.get('name', 'your plan')} open the Plans tab "
            f"to see today's full meal schedule. Try some water while you wait! 💧"
        )

    identity   = (profile or {}).get("identity") or {}
    name       = identity.get("fullName") or profile.get("name") or ""
    first_name = name.split()[0] if name else ""

    prompt = f"""You are a short, warm nutrition guide. The user says they're hungry.

Their active plan's meals for today are:
{meals_text}

Their name is: {first_name or 'the user'}

Rules:
- Suggest something they can eat RIGHT NOW based on what's in their plan above
- Do NOT invent food outside the plan
- If the plan has a light meal or something suitable as a snack (fruits, nuts, light item), suggest that
- If all meals are heavy, suggest the lightest option or a portion of it, and mention drinking water first
- Reply in 2–3 sentences max. Warm and direct. Do NOT start with "Hey [name]!"
- No bullet lists

Trainer response:"""

    try:
        resp = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return _truncate_gemini_answer(resp.text.strip(), max_sentences=3)
    except Exception as e:
        logger.error(f"❌ _hunger_response_paid Gemini error: {e}")
        return (
            "Check your plan's meals in the Plans tab pick the lightest item available "
            "and have some water alongside it! 💧"
        )


def _hunger_response_free(profile: dict) -> str:
    """
    For free users: use Gemini to give a short, enticing overview of available plans
    and gently encourage them to get one with actual plan names from DB.
    """
    plans_text = _build_available_plans_text(profile)

    identity   = (profile or {}).get("identity") or {}
    name       = identity.get("fullName") or profile.get("name") or ""
    first_name = name.split()[0] if name else ""

    food = (profile or {}).get("foodactivity") or {}
    prefs = food.get("foodPreferences") or []
    diet_hint = f"Their diet preference: {', '.join(prefs)}." if prefs else ""

    plans_section = f"Available plans in the app:\n{plans_text}" if plans_text else \
                    "The app has expert-designed diet and ration plans by Dr. Meghana Kumare."

    prompt = f"""You are a short, warm nutrition guide. The user says they're hungry but has no active plan.

{plans_section}

User name: {first_name or 'the user'}
{diet_hint}

Rules:
- Briefly mention 1–2 relevant plan names from the list above (if available) that match their diet preference
- Give ONE short benefit of being on a plan (e.g. knowing exactly what to eat)
- End with a friendly nudge to check the Plans tab
- Do NOT suggest specific foods or snacks since they have no plan
- 2–3 sentences max. Warm and direct. Do NOT start with "Hey [name]!"
- No bullet lists

Trainer response:"""

    try:
        resp = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return _truncate_gemini_answer(resp.text.strip(), max_sentences=3)
    except Exception as e:
        logger.error(f"❌ _hunger_response_free Gemini error: {e}")
        return (
            "Getting on a plan means you'll always know exactly what to eat no guessing! "
            "Check the Plans tab to explore Dr. Meghana's options. 😊"
        )


def _hunger_response(profile: Optional[dict]) -> str:
    """Route hunger intent to paid or free handler."""
    if get_user_plan_status(profile) == 'paid':
        return _hunger_response_paid(profile)
    return _hunger_response_free(profile or {})


def detect_goal_from_history(history: list) -> Optional[str]:
    goal_keywords = {
        "weight loss":     ["lose weight", "weight loss", "slim down", "fat loss", "cut"],
        "weight gain":     ["gain weight", "bulk", "weight gain", "gain mass"],
        "muscle building": ["build muscle", "muscle gain", "strength", "bulk up"],
        "maintenance":     ["maintain", "stay fit", "healthy lifestyle", "eat healthy"],
    }
    for msg in reversed(history[-20:] if history else []):
        text = (msg.get("question", "") + " " + msg.get("answer", "")).lower()
        for goal, keywords in goal_keywords.items():
            if any(kw in text for kw in keywords):
                return goal
    return None


# ── Short canned responses ────────────────────────────────────────────────────

def _greeting_response(profile: Optional[dict]) -> str:
    identity   = (profile or {}).get("identity") or {}
    name       = identity.get("fullName") or (profile or {}).get("name") or ""
    first_name = name.split()[0] if name else ""
    addr       = f", {first_name}" if first_name else ""
    plan_status = get_user_plan_status(profile)
    if plan_status == 'paid':
        active = get_active_plan_summary(profile)
        pname  = active.get("name", "your plan") if active else "your plan"
        return (
            f"Hey{addr}! 👋 Good to see you — you're on {pname} right now. "
            f"What can I help you with today? 😊"
        )
    return (
        f"Hey{addr}! 👋 I'm your nutrition guide at Red Apple Wellness Diet Center. "
        f"Whether it's weight loss, muscle gain, or eating better I'm here to help. "
        f"What's on your mind? 😊"
    )


def _consult_response(profile: Optional[dict]) -> str:
    identity = (profile or {}).get("identity") or {}
    name     = identity.get("fullName") or (profile or {}).get("name") or ""
    first    = name.split()[0] if name else ""
    addr     = f", {first}" if first else ""
    return (
        f"Sure{addr}! Dr. Meghana would be happy to help with a personal consultation. "
        f"Reach her at +91 7774944783 or rawdiets@gmail.com use the WhatsApp button in the app to connect directly. 😊"
    )


# ── App navigation intents ────────────────────────────────────────────────────

NAV_INTENTS = [
    {
        "id": "bmi_bmr",
        "keywords": [
            "bmi", "bmr", "body mass index", "basal metabolic rate",
            "how to check bmi", "calculate bmi", "calculate bmr",
            "bmi calculator", "bmr calculator", "health tools",
            "check my bmi", "what is my bmi",
        ],
        "answer": (
            "📊 *BMI & BMR Calculator*\n\n"
            "Here's how to find it:\n"
            "1️⃣ Go to the *Dashboard*\n"
            "2️⃣ Tap on *Health Tools*\n"
            "3️⃣ Select *BMI & BMR Calc*\n\n"
            "There you can calculate your Body Mass Index and Basal Metabolic Rate "
            "based on your current height and weight. It updates whenever you update your profile! 💪"
        ),
    },
    {
        "id": "water_tracker",
        "keywords": [
            "water tracker", "track water", "water intake", "log water",
            "how to track water", "water goal", "daily water",
            "where is water", "water log", "hydration tracker",
            "how much water", "water reminder",
        ],
        "answer": (
            "💧 *Water Tracker*\n\n"
            "Here's how to log your water intake:\n"
            "1️⃣ Go to the *Home* tab\n"
            "2️⃣ Tap on *Water Tracker*\n\n"
            "You can log every glass you drink throughout the day and track it "
            "against your daily water goal. Staying hydrated is a huge part of your plan! 🌊"
        ),
    },
    {
        "id": "progress",
        "keywords": [
            "my progress", "check progress", "view progress", "track progress",
            "how to see progress", "diet progress", "progress report",
            "progress tracker", "where is my progress", "see my progress",
            "progress chart", "how am i doing",
        ],
        "answer": (
            "📈 *My Progress*\n\n"
            "Here's how to review your diet progress:\n"
            "1️⃣ Go to the *Home* tab\n"
            "2️⃣ Tap on *My Progress*\n\n"
            "You'll see a full overview of your diet plan progress — "
            "days completed, meals followed, and how far you've come on your journey. "
            "Keep going, every day counts! 🏆"
        ),
    },
    {
        "id": "explore_plans",
        "keywords": [
            "explore plans", "find plans", "browse plans", "see plans",
            "available plans", "diet plans", "which plans", "show me plans",
            "how to buy plan", "how to activate plan", "purchase plan",
            "where to buy", "explore diet", "check plans", "view plans",
            "how to find plan", "where are plans",
        ],
        "answer": (
            "🥗 *Explore Diet Plans*\n\n"
            "Here's how to find and activate a plan:\n"
            "1️⃣ Tap the *Diet* tab at the bottom of the screen\n"
            "2️⃣ Browse all available plans and filter by your goal or diet type\n"
            "3️⃣ Tap any plan to see full details duration, meals, and what's included\n"
            "4️⃣ Purchase the plan that fits you to activate it instantly ✅\n\n"
            "Once active, your plan meals will appear in the Home tab every day. "
            "Not sure which plan suits you? Just ask me and I'll help you choose! 😊"
        ),
    },
    {
        "id": "recipes",
        "keywords": [
            "recipe", "recipes", "find recipe", "browse recipe", "see recipes",
            "where are recipes", "how to find recipe", "recipe tab",
            "different recipes", "check recipes", "all recipes",
            "recipe list", "view recipes", "explore recipes",
        ],
        "answer": (
            "🍳 *Recipes*\n\n"
            "Here's how to explore all recipes:\n"
            "1️⃣ Tap the *Recipe* tab at the bottom of the screen\n"
            "2️⃣ Browse hundreds of recipes filter by meal type, diet preference, or ingredients\n"
            "3️⃣ Tap any recipe to see full ingredients, step-by-step instructions, and nutrition info\n\n"
            "You can also swap recipes within your active plan if you want variety. "
            "Found something you like? Your plan's meals are always a good starting point! ��"
        ),
    },
    {
        "id": "health_profile",
        "keywords": [
            "update health", "health updates", "add health", "remove health",
            "update profile", "edit profile", "health profile",
            "change my details", "update my details", "edit my details",
            "update weight", "update height", "health conditions",
            "profile settings", "where to update", "how to update profile",
            "change health info", "medical details",
        ],
        "answer": (
            "👤 *Health Profile*\n\n"
            "Here's how to update your health details:\n"
            "1️⃣ Go to your *Profile* (tap your profile icon)\n"
            "2️⃣ You can add or update:\n"
            "   • Weight & height\n"
            "   • Health conditions\n"
            "   • Diet preferences & allergies\n"
            "   • Activity level and sleep habits\n"
            "3️⃣ Save your changes and the AI will use your latest details instantly ✅\n\n"
            "Keeping your profile updated ensures your plan and guidance stay accurate for you!"
        ),
    },
]


def is_nav_intent(message: str) -> Optional[str]:
    """
    Check if message matches a navigation intent.
    Returns the answer string if matched, else None.
    Checks all keywords across all nav intents.
    """
    msg = message.lower().strip()
    for intent in NAV_INTENTS:
        for kw in intent["keywords"]:
            if kw in msg:
                logger.info(f"✅ Nav intent matched: {intent['id']} (keyword: '{kw}')")
                return intent["answer"]
    return None


# ── Main answer functions ──────────────────────────────────────────────────────

def _load_profile(firebase_uid, firebase_token):
    """Load profile from DB (preferred) or API fallback."""
    profile = None
    if firebase_uid:
        try:
            from database import SessionLocal
            _db = SessionLocal()
            try:
                profile = fetch_user_profile_db(firebase_uid, _db)
            finally:
                _db.close()
        except Exception as e:
            logger.warning(f"⚠️ DB profile load failed: {e}")
    if not profile:
        profile = fetch_user_profile(firebase_token)
    return profile


def get_answer(
    question: str,
    session_id: Optional[str] = None,
    db_session=None,
    firebase_token: Optional[str] = None,
    firebase_uid: Optional[str] = None,
) -> str:
    global gemini_client

    if gemini_client is None:
        return "Having a little trouble connecting right now — give it a moment and try again! 🙏"

    profile = _load_profile(firebase_uid, firebase_token)

    if is_greeting(question):
        return _greeting_response(profile)

    if is_consult_intent(question):
        return _consult_response(profile)

    if is_hunger_intent(question):
        return _hunger_response(profile)

    nav_answer = is_nav_intent(question)
    if nav_answer:
        return nav_answer

    try:
        history = []
        if db_session and session_id:
            try:
                history = get_recent_messages(db_session, session_id, limit=10)
            except Exception as e:
                logger.warning(f"Could not load history: {e}")

        goal   = detect_goal_from_history(history)
        prompt = build_prompt(question, profile, history, goal)

        logger.info("🤖 Calling Gemini for answer...")
        resp   = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        answer = _truncate_gemini_answer(resp.text.strip())
        logger.info(f"✅ Answer ready ({len(answer)} chars)")
        return answer

    except Exception as e:
        logger.error(f"❌ get_answer error: {e}\n{traceback.format_exc()}")
        return "Something came up on my end — let's try that again in a second! 💪"


def get_answer_stream(
    question: str,
    session_id: Optional[str] = None,
    db_session=None,
    firebase_token: Optional[str] = None,
    firebase_uid: Optional[str] = None,
) -> Generator[str, None, None]:
    import json as _j

    def sse(payload: dict) -> str:
        return f"data: {_j.dumps(payload)}\n\n"

    if gemini_client is None:
        msg = "Having a little trouble connecting right now — give it a moment and try again! 🙏"
        yield sse({"type": "chunk", "text": msg})
        yield sse({"type": "done",  "text": ""})
        return

    profile = _load_profile(firebase_uid, firebase_token)

    if is_greeting(question):
        msg = _greeting_response(profile)
        yield sse({"type": "chunk", "text": msg})
        yield sse({"type": "done",  "text": "", "full_text": msg})
        return

    if is_consult_intent(question):
        msg = _consult_response(profile)
        yield sse({"type": "chunk", "text": msg})
        yield sse({"type": "done",  "text": "", "full_text": msg})
        return

    if is_hunger_intent(question):
        msg = _hunger_response(profile)
        yield sse({"type": "chunk", "text": msg})
        yield sse({"type": "done",  "text": "", "full_text": msg})
        return

    nav_answer = is_nav_intent(question)
    if nav_answer:
        yield sse({"type": "chunk", "text": nav_answer})
        yield sse({"type": "done",  "text": "", "full_text": nav_answer})
        return

    try:
        history = []
        if db_session and session_id:
            try:
                history = get_recent_messages(db_session, session_id, limit=10)
            except Exception as e:
                logger.warning(f"Could not load history: {e}")

        goal        = detect_goal_from_history(history)
        prompt      = build_prompt(question, profile, history, goal)
        accumulated = ""

        logger.info("🤖 Streaming from Gemini...")
        stream = gemini_client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=prompt
        )
        for chunk in stream:
            if chunk.text:
                accumulated += chunk.text
                yield sse({"type": "chunk", "text": chunk.text})

        final = _truncate_gemini_answer(accumulated.strip())
        logger.info(f"✅ Stream complete ({len(final)} chars)")
        # full_text is used by main.py to save to DB
        # text is empty so the frontend does NOT render it again (prevents duplicate)
        yield sse({"type": "done", "text": "", "full_text": final})

    except Exception as e:
        logger.error(f"❌ get_answer_stream error: {e}\n{traceback.format_exc()}")
        yield sse({"type": "error", "text": "Something came up on my end — let's try that again in a second! 💪"})


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
        history_pairs = []
        i = 0
        while i < len(rows) - 1:
            if rows[i].role == MessageRole.USER and rows[i + 1].role == MessageRole.ASSISTANT:
                history_pairs.append({
                    "question": rows[i].content,
                    "answer":   rows[i + 1].content,
                })
                i += 2
            else:
                i += 1
        return history_pairs[-limit:]
    except Exception as e:
        logger.error(f"❌ get_recent_messages error: {e}")
        return []
