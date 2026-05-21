"""
whatsapp_redirect.py

Builds a WhatsApp deep-link (wa.me) pre-filled with the user's full profile
that is already stored in the Raw Diet backend database.

NO Twilio. NO external API.
The chatbot returns a special JSON response that includes:
  - a human-readable bot reply
  - a `whatsapp_url`  field the frontend uses to render a "Chat on WhatsApp" button

When the user taps that button the phone opens WhatsApp with Meghana's number
and the full profile message already typed — they just hit Send.
"""

import urllib.parse
from typing import Optional

CLINIC_WHATSAPP = "917774944783"   # country code, no +, no spaces — wa.me format


# ─────────────────────────────────────────────────────────────────────────────
# Message builder  —  reads the exact same profile dict that fetch_user_profile()
# returns from  GET /api/users/me  (see rag_engine.py)
# ─────────────────────────────────────────────────────────────────────────────

def build_whatsapp_message(profile: Optional[dict]) -> str:
    """
    Converts the user profile (as returned by /api/users/me) into a
    WhatsApp-friendly plain-text message for Dr. Meghana.

    Fields mapped from schema.prisma:
      Identity     → name, age, gender, height, weight, contact, blood group,
                     occupation, address, marital status, DOB
      HealthConds  → conditions, otherDetails, treatmentTaken, menstrualHistory,
                     bowelBladder, sleepTime, sleepQuality
      FoodActivity → foodPreferences, allergies, cravings, dietaryRestrictions,
                     activityLevel, activities, morning/breakfast/lunch/snacks/dinner
      FamilyHealth → familyType, members, familyHistory, waterIntake
    """
    if not profile:
        return (
            "Hello Dr. Meghana,\n\n"
            "I am interested in a personalised diet consultation.\n"
            "Please guide me further.\n\n"
            "Thank you!"
        )

    lines = []

    # ── Identity ──────────────────────────────────────────────────────────────
    identity = profile.get("identity") or {}
    name     = identity.get("fullName") or profile.get("name") or "User"

    lines.append(f"🍎 *New Diet Consultation — Raw Diet App*")
    lines.append(f"")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"👤 *PERSONAL DETAILS*")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Name        : {name}")

    if identity.get("age"):
        lines.append(f"Age         : {identity['age']} years")
    if identity.get("gender"):
        lines.append(f"Gender      : {identity['gender'].title()}")
    if identity.get("dateOfBirth"):
        lines.append(f"Date of Birth: {identity['dateOfBirth'][:10]}")
    if identity.get("bloodGroup"):
        bg = identity["bloodGroup"].replace("_POS", "+").replace("_NEG", "-")
        lines.append(f"Blood Group : {bg}")
    if identity.get("maritalStatus"):
        lines.append(f"Marital     : {identity['maritalStatus'].title()}")
    if identity.get("occupation"):
        lines.append(f"Occupation  : {identity['occupation']}")
    if identity.get("address"):
        lines.append(f"Address     : {identity['address']}")
    if identity.get("contact"):
        lines.append(f"Contact     : {identity['contact']}")

    # ── Body Stats ────────────────────────────────────────────────────────────
    height_cm = identity.get("heightCm")
    weight_kg = identity.get("weightKg")
    if height_cm or weight_kg:
        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📏 *BODY STATS*")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        if height_cm:
            lines.append(f"Height : {height_cm} cm")
        if weight_kg:
            lines.append(f"Weight : {weight_kg} kg")
        if height_cm and weight_kg:
            bmi = round(weight_kg / ((height_cm / 100) ** 2), 1)
            lines.append(f"BMI    : {bmi}")

    # ── Health Conditions ─────────────────────────────────────────────────────
    health = profile.get("health") or {}
    if health:
        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🏥 *HEALTH CONDITIONS*")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        conds = health.get("conditions") or []
        lines.append(f"Conditions   : {', '.join(conds) if conds else 'None'}")
        if health.get("otherDetails"):
            lines.append(f"Other Details: {health['otherDetails']}")
        if health.get("treatmentTaken"):
            lines.append(f"Treatment    : {health['treatmentTaken']}")
        if health.get("menstrualHistory"):
            lines.append(f"Menstrual    : {health['menstrualHistory']}")
        if health.get("bowelBladder"):
            lines.append(f"Bowel/Bladder: {health['bowelBladder']}")
        if health.get("sleepTime"):
            lines.append(f"Sleep Time   : {health['sleepTime']}")
        if health.get("sleepQuality"):
            lines.append(f"Sleep Quality: {health['sleepQuality'].title()}")

    # ── Food, Activity & Diet ─────────────────────────────────────────────────
    food = profile.get("foodactivity") or {}
    if food:
        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🥗 *DIET & ACTIVITY*")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        prefs = food.get("foodPreferences") or []
        if prefs:
            lines.append(f"Diet Type    : {', '.join(p.replace('_', '-') for p in prefs)}")
        allergies = food.get("allergies") or profile.get("allergies") or []
        lines.append(f"Allergies    : {', '.join(allergies) if allergies else 'None'}")
        if food.get("cravings"):
            lines.append(f"Cravings     : {food['cravings']}")
        if food.get("dietaryRestrictions"):
            lines.append(f"Restrictions : {food['dietaryRestrictions']}")
        if food.get("activityLevel"):
            lines.append(f"Activity Lvl : {food['activityLevel'].replace('_', ' ').title()}")
        acts = food.get("activities") or []
        if acts:
            lines.append(f"Activities   : {', '.join(a.replace('_', ' ') for a in acts)}")

        # Current daily menu
        meals = {
            "Morning"  : food.get("morning"),
            "Breakfast": food.get("breakfast"),
            "Lunch"    : food.get("lunch"),
            "Snacks"   : food.get("snacks"),
            "Dinner"   : food.get("dinner"),
        }
        filled_meals = {k: v for k, v in meals.items() if v}
        if filled_meals:
            lines.append(f"")
            lines.append(f"🍽️ *Current Daily Menu*")
            for meal_name, meal_val in filled_meals.items():
                lines.append(f"{meal_name:10}: {meal_val}")

    # ── Family Health ─────────────────────────────────────────────────────────
    family = profile.get("familyHealth") or {}
    if family:
        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"👨‍👩‍👧 *FAMILY HEALTH*")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
        if family.get("familyType"):
            lines.append(f"Family Type  : {family['familyType'].title()}")
        if family.get("members"):
            lines.append(f"Members      : {family['members']}")
        if family.get("familyHistory"):
            lines.append(f"Family Illness: {family['familyHistory']}")
        if family.get("waterIntake"):
            wi_map = {
                "LESS_THAN_1L": "< 1 Litre",
                "ONE_TO_TWO_L": "1–2 Litres",
                "TWO_TO_THREE_L": "2–3 Litres",
                "MORE_THAN_3L": "> 3 Litres",
            }
            lines.append(f"Water Intake : {wi_map.get(family['waterIntake'], family['waterIntake'])}")

    lines.append(f"")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"_Sent via Raw Diet App_")

    return "\n".join(lines)


def build_whatsapp_url(profile: Optional[dict]) -> str:
    """
    Returns a wa.me deep-link URL.
    Opens WhatsApp on the user's phone with Dr. Meghana's number pre-selected
    and the full profile message pre-filled in the text box.
    The user just taps Send — no typing needed.
    """
    message = build_whatsapp_message(profile)
    encoded = urllib.parse.quote(message)
    return f"https://wa.me/{CLINIC_WHATSAPP}?text={encoded}"


# ─────────────────────────────────────────────────────────────────────────────
# Trigger detection  —  when should the bot offer the WhatsApp button?
# ─────────────────────────────────────────────────────────────────────────────

WHATSAPP_TRIGGER_KEYWORDS = [
    "diet plan", "meal plan", "diet chart", "diet schedule",
    "what should i eat", "food plan", "nutrition plan",
    "weight loss plan", "weight gain plan",
    "suggest a diet", "create a plan", "make a plan", "design a diet",
    "personalised plan", "personalized plan", "custom plan", "custom diet",
    "consult", "consultation", "book", "appointment",
    "talk to doctor", "talk to dietician", "talk to meghana",
    "speak to", "connect me", "contact",
    "full plan", "complete plan", "detailed plan",
    "help me lose", "help me gain",
]

def should_offer_whatsapp(message: str) -> bool:
    """Returns True when the user's message should trigger the WhatsApp CTA."""
    msg = message.lower()
    return any(kw in msg for kw in WHATSAPP_TRIGGER_KEYWORDS)

