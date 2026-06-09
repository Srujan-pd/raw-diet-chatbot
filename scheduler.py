"""
scheduler.py — Daily Morning Message for Paid Users

How it works:
  1. Cloud Scheduler hits POST /scheduler/daily-morning at 7:00 AM IST every day
  2. This module queries all paid users who have an active ChatSession
  3. For each user it builds a personalised morning message using their profile
     (name, goal, water intake target, meal windows from sleep time)
  4. Inserts that message as an ASSISTANT message into their ChatSession
  5. Next time the user opens the chatbot → they see it waiting, like Riya messaged them

No FCM. No WhatsApp API. No external service. Pure DB insert.

Duplicate guard:
  Before inserting, checks if a morning message was already sent today
  (looks for a message with meta.type = "morning_greeting" created today).
  Safe to call multiple times — will not double-insert.
"""

import json
import logging
import os
import uuid
import urllib.request
from datetime import datetime, date, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal
from models import ChatMessage, ChatSession, MessageRole

logger = logging.getLogger(__name__)

RAW_DIET_API_BASE = os.getenv(
    "RAW_DIET_API_BASE",
    "https://test---raw-diet-backend-5rnsarrnya-uc.a.run.app"
)

# ── IST timezone ───────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))


# ── Meal window calculator ─────────────────────────────────────────────────────

def get_meal_windows(sleep_time: Optional[str]) -> dict:
    """
    Derive meal windows from the user's sleep time stored in their profile.
    sleep_time is stored as a string like "10:00 PM", "22:00", "11 PM" etc.
    Falls back to sensible defaults if missing or unparseable.
    """
    # Defaults (used when sleep_time is missing)
    defaults = {
        "breakfast": "7:30 – 8:30 AM",
        "lunch":     "12:30 – 1:30 PM",
        "snack":     "4:30 – 5:30 PM",
        "dinner":    "7:30 – 8:30 PM",
    }

    if not sleep_time:
        return defaults

    # Try to parse wake time as sleep_time - 8 hours
    try:
        clean = sleep_time.strip().upper().replace(".", "").replace("  ", " ")
        # Handle formats: "10:00 PM", "10 PM", "22:00", "2200"
        if "AM" in clean or "PM" in clean:
            for fmt in ["%I:%M %p", "%I %p"]:
                try:
                    t = datetime.strptime(clean, fmt)
                    break
                except ValueError:
                    continue
            else:
                return defaults
        else:
            clean = clean.replace(":", "")
            hour = int(clean[:2]) if len(clean) >= 2 else int(clean)
            minute = int(clean[2:4]) if len(clean) >= 4 else 0
            t = datetime.now(IST).replace(hour=hour, minute=minute)

        # Wake time = sleep time + 8 hours
        sleep_dt = datetime.now(IST).replace(
            hour=t.hour, minute=t.minute, second=0, microsecond=0
        )
        wake_dt = sleep_dt + timedelta(hours=8)
        wh, wm = wake_dt.hour, wake_dt.minute

        def fmt_window(base_h, base_m, span=60):
            start = datetime.now(IST).replace(hour=base_h, minute=base_m)
            end   = start + timedelta(minutes=span)
            return f"{start.strftime('%-I:%M')} – {end.strftime('%-I:%M %p')}"

        # Breakfast: wake + 30 min
        bf_h = (wh + (wm + 30) // 60) % 24
        bf_m = (wm + 30) % 60

        # Lunch: wake + 4.5 hrs
        lu_h = (wh + (wm + 270) // 60) % 24
        lu_m = (wm + 270) % 60

        # Snack: wake + 7.5 hrs
        sn_h = (wh + (wm + 450) // 60) % 24
        sn_m = (wm + 450) % 60

        # Dinner: wake + 11 hrs
        di_h = (wh + (wm + 660) // 60) % 24
        di_m = (wm + 660) % 60

        return {
            "breakfast": fmt_window(bf_h, bf_m),
            "lunch":     fmt_window(lu_h, lu_m),
            "snack":     fmt_window(sn_h, sn_m),
            "dinner":    fmt_window(di_h, di_m),
        }

    except Exception as e:
        logger.warning(f"Could not parse sleep_time '{sleep_time}': {e}")
        return defaults


# ── Water target from profile ──────────────────────────────────────────────────

def get_water_target(water_intake_enum: Optional[str]) -> str:
    mapping = {
        "LESS_THAN_1L":    "at least 2L",
        "ONE_TO_TWO_L":    "at least 2.5L",
        "TWO_TO_THREE_L":  "2.5 – 3L",
        "MORE_THAN_3L":    "3L or more",
    }
    return mapping.get(water_intake_enum or "", "at least 2.5L")


# ── 100 Motivational quotes (one per day, cycles after 100) ─────────────────

MOTIVATIONS = [
    # Weight loss
    "Every clean meal is a vote for the person you're becoming. Keep voting! 🔥",
    "Progress is progress — even 1% better today matters. You've got this! 💪",
    "Your body is changing even when you can't see it yet. Trust the process! 🌱",
    "Small choices compound. Every good meal today = a better you tomorrow! ⚡",
    "You didn't come this far to only come this far. Keep pushing! 🏆",
    "The scale doesn't measure your strength, discipline, or courage. You have all three! 💫",
    "Consistency is the secret ingredient no one talks about. You're nailing it! 🎯",
    "Eating right today is an act of love for your future self. Keep going! 💚",
    "Results happen in the background while you stay consistent. Stay the course! 🌿",
    "The hardest part is showing up — and you already did that! 🙌",
    # Muscle / strength
    "Strength is built one meal and one rep at a time. Today counts! 💥",
    "Fuel your body like the machine it is. Protein, hydration, rest — nail it! 🥊",
    "Champions are built on the days they don't feel like it. Show up anyway! 🏆",
    "Your nutrition today is your performance tomorrow. Choose wisely! ⚡",
    "Every gram of protein is a brick in the wall of your strength. Build it! 🧱",
    "Muscle isn't built in the gym alone — it's built at the table too. Eat strong! 🍽️",
    "Recovery starts with what you eat. Make today's meals count! 🌟",
    "Strong people are made in the kitchen first. You're building something great! 💪",
    "Discipline is choosing what you want most over what you want right now. Choose! 🎯",
    "Your body responds to every good choice you make. Give it the best today! ⚡",
    # Weight gain
    "Growth requires fuel — eat with intention today! 🏋️",
    "Every meal is an opportunity to build the body you're working toward! 🧱",
    "Rest, eat, grow — that's the formula. Trust it and stay consistent! 🌟",
    "Your gains start in the kitchen. Show up at the table today! 🍽️",
    "Quality calories are the foundation of everything. Fuel right today! 💪",
    # General health & wellness
    "A healthy day starts with a healthy mindset. You've already won half the battle! ☀️",
    "Nourish your body — it's the only home you'll ever truly live in! 🌿",
    "One good day at a time. That's all it takes. Today is yours! 🌱",
    "Eat well, move well, feel well. It's that simple — and you're doing it! 💚",
    "Your health is your greatest wealth. Invest in it generously today! 💛",
    "Every sunrise is a fresh start. Make this one count! 🌅",
    "You are what you consistently do. Today, be excellent! ⭐",
    "Real results come to those who show up even when it's hard. That's you! 🙌",
    "Your future self is watching every decision you make today. Make them proud! 🌟",
    "Healthy living isn't a phase — it's the life you're choosing every single day! 🔥",
    "When you feel like giving up, remember why you started. Hold on! 💪",
    "The food you eat today is medicine or poison. You choose — and you always choose well! 🌿",
    "Hydration, nutrition, sleep — the holy trinity of transformation. Honour all three! 💧",
    "Your commitment to your health inspires everyone around you — even if you don't know it! ✨",
    "Discipline is the bridge between goals and achievement. You're walking it! 🏃",
    "Some days are hard. Hard days are the ones that build the most character! 💫",
    "You are stronger than your cravings — and you prove it every day! 🎯",
    "The version of you at the end of this plan is going to thank you. Keep going! 🏆",
    "Wellness is not a destination — it's how you travel. Travel well today! 🛤️",
    "A little progress each day adds up to big results. Today's progress matters! 📈",
    "Comparison is the thief of progress. Run your own race — and run it well! 🏃",
    "You don't need motivation every day — you need habits. Your habits are forming! 🌱",
    "Food is information for your body. Send it the best signals today! 📡",
    "Rest is not giving up — it's fuelling the next push forward. Rest and rise! 🌙",
    "You are not starting over — you are continuing forward. Keep moving! ➡️",
    # Mindset
    "Success in health is a thousand small choices. Make yours count today! 🎯",
    "The pain of discipline is nothing compared to the pain of regret. Stay disciplined! ⚡",
    "Don't wish for it — work for it, eat for it, sleep for it! 🌟",
    "Your body hears everything your mind says. Tell it great things today! 🧠",
    "Transform your habits and you transform your life. One day at a time! 🦋",
    "Nobody regrets eating healthy. Ever. Keep that in mind today! 💚",
    "The secret to getting ahead is getting started — you already did that! 🏁",
    "Energy flows where attention goes. Focus on your health today! 🔋",
    "You are not on a diet — you are on a journey to the best version of you! 🛤️",
    "Your only competition is who you were yesterday. Beat that person today! 🏆",
    # Fun & Light
    "Water first. Always water first. Your cells are literally cheering you on! 💧",
    "Eat the rainbow today — and no, Skittles don't count 😄 Veggies do! 🌈",
    "Your gut is talking to you — feed it something amazing today! 🌿",
    "Think of every meal as a high five to your future self! 🖐️",
    "Breakfast is the opening scene of your health movie today — make it a blockbuster! 🎬",
    "Your body is rooting for you 24/7. Give it the fuel it deserves! ⚡",
    "Good food is the foundation of genuine happiness. Eat well, be happy! 😊",
    "You are one meal away from being in a good mood. Choose that meal wisely! 🍽️",
    "Every vegetable you eat is a tiny superhero fighting for your health! 🦸",
    "Drink your water, eat your protein, sleep your 8 hours. You're a health machine! 🤖",
    # Streaks & Consistency
    "Another day, another step forward. Your streak is proof of your commitment! ��",
    "Streaks aren't just numbers — they're a visual record of who you're becoming! 📈",
    "Each day you follow your plan is a day your body says thank you! 🙏",
    "Momentum is your superpower. Don't let it stop today! ⚡",
    "Day by day — that's how legends are made. You're building your legend! 🌟",
    "The longer your streak, the harder it is to break. Keep it alive! 💪",
    "Every meal completed is a vote for the streak. Vote yes today! ✅",
    "Staying consistent is the most powerful thing you can do for your health! 🎯",
    "You're not just following a plan — you're building an unbreakable identity! 🏆",
    "Habits are built in the moments you push through. Today is one of those moments! 💫",
    # Seasonal / general
    "This is the version of you who shows up. Today is another chapter! 📖",
    "The journey of a thousand miles begins with a single meal. Eat it well! ��",
    "Health is a gift you give yourself — unwrap it every single day! 🎁",
    "You're not just losing weight / gaining strength — you're gaining life! ✨",
    "The best investment you'll ever make is in your own health. Invest today! 💰",
    "Make your body your best project. It's already in progress! 🛠️",
    "Every plan follower started where you are. Every legend stayed consistent. Be both! 🌟",
    "You are the author of your health story. Write a great chapter today! ✍️",
    "Obstacles are just detours in the right direction. Stay on your path! 🛤️",
    "Be patient with yourself — great things take time, consistency, and good food! 🌱",
    "Don't count the days — make the days count! ⏳",
    "You already made the hardest decision — to start. Now just keep going! 🚀",
    "Health doesn't happen to you — it happens because of you. Own it today! 💪",
    "The mirror isn't the only measure of progress. How do you feel today? That matters! 💫",
    "One more day of consistency and you're one day closer to your goal. That's maths! 🔢",
    "Dr. Meghana designed this plan for *you*. Trust it. Follow it. See magic happen! ✨",
    "Goals don't care about your mood. Show up anyway — your future self will thank you! 🏆",
    "You've already proven you can do hard things. Today is just another proof! 💪",
]


def get_motivation(goal: Optional[str], day_number: int) -> str:
    """
    Returns a different motivational quote each day.
    Cycles through 100 quotes using the day number.
    Goal-aware: tries to pick from a relevant section first.
    """
    goal_lower = (goal or "").lower()
    idx        = (day_number - 1) % len(MOTIVATIONS)

    # For strong goal signals, bias the starting index toward relevant quotes
    if "loss" in goal_lower or "fat" in goal_lower or "slim" in goal_lower:
        idx = (day_number - 1) % 10               # quotes 0-9: weight loss focused
    elif "muscle" in goal_lower or "strength" in goal_lower:
        idx = 10 + (day_number - 1) % 10          # quotes 10-19: muscle focused
    elif "gain" in goal_lower or "bulk" in goal_lower:
        idx = 20 + (day_number - 1) % 5           # quotes 20-24: weight gain focused
    else:
        idx = 25 + (day_number - 1) % 75          # quotes 25-99: general

    return MOTIVATIONS[idx % len(MOTIVATIONS)]


# ── Morning message builder ────────────────────────────────────────────────────

def build_morning_message(profile: dict, day_number: int) -> str:
    """
    Builds the personalised morning message for a paid user.
    Uses their name, goal, sleep time, water intake from their stored profile.
    """
    identity = profile.get("identity") or {}
    food     = profile.get("foodactivity") or {}
    family   = profile.get("familyHealth") or {}
    health   = profile.get("health") or {}

    # Name
    full_name  = identity.get("fullName") or profile.get("name") or "there"
    first_name = full_name.split()[0] if full_name and full_name != "there" else full_name

    # Goal — try to detect from health conditions or food preferences
    # (real goal field would come from the plan — using best available signal)
    conditions = health.get("conditions") or []
    prefs      = food.get("foodPreferences") or []
    goal       = None  # Will be enriched once plan data is available

    # Water target
    water_target = get_water_target(family.get("waterIntake"))

    # Meal windows from sleep time
    sleep_time = health.get("sleepTime")
    meals      = get_meal_windows(sleep_time)

    # Day display
    day_str = f"Day {day_number}" if day_number > 0 else "a new day"

    # Motivation
    motivation = get_motivation(goal, day_number)

    # Today's date in a friendly format
    today = datetime.now(IST).strftime("%A, %d %B")

    message = (
        f"Good morning, {first_name}! 🌅\n\n"
        f"It's {today} — {day_str} of your plan. Here's your focus for today:\n\n"
        f"💧 *Water goal* — Drink {water_target} today\n"
        f"🍳 *Breakfast* — {meals['breakfast']}\n"
        f"🍽️ *Lunch* — {meals['lunch']}\n"
        f"🥗 *Evening snack* — {meals['snack']}\n"
        f"🌙 *Dinner* — {meals['dinner']}\n\n"
        f"💪 {motivation}\n\n"
        f"Ask me anything about your meals, nutrition, or how you're feeling today. "
        f"I'm right here! 😊"
    )
    return message


def build_morning_message_with_streak(
    profile: dict,
    day_number: int,
    current_streak: int,
    longest_streak: int,
) -> str:
    """
    Enhanced morning message that includes streak info.
    Called when streak data is available.
    """
    base = build_morning_message(profile, day_number)

    if current_streak <= 0:
        return base

    # Append streak line before the closing line
    if current_streak == 1:
        streak_line = "🔥 *Streak* — Day 1! A fresh start — let's build something great!"
    elif current_streak < 7:
        streak_line = f"🔥 *Streak* — {current_streak} days strong! Keep the fire burning!"
    elif current_streak < 15:
        streak_line = f"🔥 *Streak* — {current_streak} days! You're in the zone now!"
    elif current_streak < 30:
        streak_line = f"⚡ *Streak* — {current_streak} days! You're unstoppable!"
    else:
        streak_line = f"�� *Streak* — {current_streak} days! Absolute legend status!"

    # Insert streak line before the last paragraph
    parts = base.rsplit("\n\n", 1)
    if len(parts) == 2:
        return parts[0] + f"\n\n{streak_line}\n\n" + parts[1]
    return base + f"\n\n{streak_line}"


# ── Fetch profile by internal user_id (no Firebase token needed) ──────────────

def fetch_profile_by_user_id(user_id: str, db: Session) -> Optional[dict]:
    """
    Fetches user profile data directly from the DB via raw SQL.
    Used by the scheduler since we don't have Firebase tokens server-side.
    Reads from Identity, HealthConditions, FoodActivity, FamilyHealth tables.
    """
    try:
        # Identity
        identity_row = db.execute(
            text("""
                SELECT "fullName", age, gender, "heightCm", "weightKg",
                       "bloodGroup", "maritalStatus", occupation, address, contact
                FROM "Identity"
                WHERE "userId" = :uid
                LIMIT 1
            """),
            {"uid": user_id}
        ).fetchone()

        # Health
        health_row = db.execute(
            text("""
                SELECT conditions, "otherDetails", "treatmentTaken",
                       "menstrualHistory", "bowelBladder", "sleepTime", "sleepQuality"
                FROM "HealthConditions"
                WHERE "userId" = :uid
                LIMIT 1
            """),
            {"uid": user_id}
        ).fetchone()

        # FoodActivity
        food_row = db.execute(
            text("""
                SELECT "foodPreferences", allergies, cravings,
                       "dietaryRestrictions", "activityLevel", activities,
                       morning, breakfast, lunch, snacks, dinner
                FROM "FoodActivity"
                WHERE "userId" = :uid
                LIMIT 1
            """),
            {"uid": user_id}
        ).fetchone()

        # FamilyHealth
        family_row = db.execute(
            text("""
                SELECT "familyType", members, "familyHistory", "waterIntake"
                FROM "FamilyHealth"
                WHERE "userId" = :uid
                LIMIT 1
            """),
            {"uid": user_id}
        ).fetchone()

        # Build profile dict matching the same shape as fetch_user_profile()
        profile = {}

        if identity_row:
            profile["identity"] = {
                "fullName":     identity_row[0],
                "age":          identity_row[1],
                "gender":       identity_row[2],
                "heightCm":     identity_row[3],
                "weightKg":     identity_row[4],
                "bloodGroup":   identity_row[5],
                "maritalStatus":identity_row[6],
                "occupation":   identity_row[7],
                "address":      identity_row[8],
                "contact":      identity_row[9],
            }

        if health_row:
            profile["health"] = {
                "conditions":      health_row[0] or [],
                "otherDetails":    health_row[1],
                "treatmentTaken":  health_row[2],
                "menstrualHistory":health_row[3],
                "bowelBladder":    health_row[4],
                "sleepTime":       health_row[5],
                "sleepQuality":    health_row[6],
            }

        if food_row:
            profile["foodactivity"] = {
                "foodPreferences":    food_row[0] or [],
                "allergies":          food_row[1] or [],
                "cravings":           food_row[2],
                "dietaryRestrictions":food_row[3],
                "activityLevel":      food_row[4],
                "activities":         food_row[5] or [],
                "morning":            food_row[6],
                "breakfast":          food_row[7],
                "lunch":              food_row[8],
                "snacks":             food_row[9],
                "dinner":             food_row[10],
            }

        if family_row:
            profile["familyHealth"] = {
                "familyType":    family_row[0],
                "members":       family_row[1],
                "familyHistory": family_row[2],
                "waterIntake":   family_row[3],
            }

        return profile if profile else None

    except Exception as e:
        logger.error(f"❌ fetch_profile_by_user_id failed for {user_id}: {e}")
        return None


# ── Paid user fetcher ──────────────────────────────────────────────────────────

def get_paid_users_with_sessions(db: Session) -> list[dict]:
    """
    Returns all users with an active ChatSession.
    No paid/free filter for now — all users get the morning message.
    Once subscription is added, update this query to filter by paid users only.
    """
    try:
        rows = db.execute(text("""
            SELECT
                cs."userId"   AS user_id,
                cs.id         AS session_id,
                cs."createdAt" AS plan_start
            FROM "ChatSession" cs
            WHERE cs."isActive" = true
            ORDER BY cs."updatedAt" DESC
        """)).fetchall()

        seen = set()
        users = []
        for row in rows:
            uid = row[0]
            if uid not in seen:
                seen.add(uid)
                users.append({
                    "user_id":    uid,
                    "session_id": row[1],
                    "plan_start": row[2],
                    "plan_name":  "Raw Diet Plan",
                })
        logger.info(f"📊 Found {len(users)} users with active sessions")
        return users

    except Exception as e:
        logger.error(f"❌ get_paid_users_with_sessions failed: {e}")
        return []


# ── Duplicate guard ────────────────────────────────────────────────────────────

def already_sent_today(db: Session, session_id: str) -> bool:
    """
    Returns True if a morning greeting was already inserted today for this session.
    Prevents duplicate messages if the scheduler runs more than once.
    Checks for messages with meta.type = 'morning_greeting' created today (IST).
    """
    try:
        today_ist_start = datetime.now(IST).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Convert to UTC for DB comparison
        today_utc_start = today_ist_start.astimezone(timezone.utc)

        row = db.execute(text("""
            SELECT id FROM "ChatMessage"
            WHERE "sessionId" = :sid
              AND role = 'ASSISTANT'
              AND metadata::jsonb ->> 'type' = 'morning_greeting'
              AND "createdAt" >= :today_start
            LIMIT 1
        """), {
            "sid":         session_id,
            "today_start": today_utc_start,
        }).fetchone()

        return row is not None

    except Exception as e:
        logger.warning(f"⚠️ already_sent_today check failed: {e}")
        return False  # If check fails, allow insert (safe fallback)


# ── Day number calculator ──────────────────────────────────────────────────────

def get_plan_day_number(plan_start) -> int:
    """Calculate how many days into their plan the user is (Day 1, Day 2, ...)"""
    try:
        if plan_start is None:
            return 1
        if isinstance(plan_start, datetime):
            start = plan_start.date()
        elif isinstance(plan_start, date):
            start = plan_start
        else:
            start = datetime.fromisoformat(str(plan_start)).date()

        today = datetime.now(IST).date()
        delta = (today - start).days + 1
        return max(1, delta)
    except Exception:
        return 1


# ── Core: insert morning message ──────────────────────────────────────────────

def insert_morning_message(
    db: Session,
    session_id: str,
    user_id: str,
    plan_start,
    plan_name: str,
) -> bool:
    """
    Fetches the user's profile, builds the morning message,
    and inserts it as an ASSISTANT message into their ChatSession.
    Returns True on success, False on failure.
    """
    try:
        # Duplicate guard
        if already_sent_today(db, session_id):
            logger.info(f"⏭️  Morning message already sent today for session {session_id}")
            return True

        # Fetch profile from DB directly (no Firebase token needed)
        profile = fetch_profile_by_user_id(user_id, db)
        if not profile:
            logger.warning(f"⚠️  No profile found for user {user_id} — skipping")
            return False

        # Calculate day number
        day_number = get_plan_day_number(plan_start)

        # Build message — with streak if available
        try:
            from streak import get_streak
            streak_data    = get_streak(db, session_id)
            current_streak = streak_data.get("current_streak", 0)
            longest_streak = streak_data.get("longest_streak", 0)
            message_text   = build_morning_message_with_streak(
                profile, day_number, current_streak, longest_streak
            )
        except Exception:
            message_text = build_morning_message(profile, day_number)

        # Insert into ChatMessage
        msg = ChatMessage(
            id        = str(uuid.uuid4()),
            sessionId = session_id,
            role      = MessageRole.ASSISTANT,
            content   = message_text,
            meta      = {
                "type":       "morning_greeting",
                "plan_name":  plan_name,
                "day_number": day_number,
                "sent_date":  datetime.now(IST).date().isoformat(),
            },
        )
        db.add(msg)
        db.commit()

        identity  = profile.get("identity") or {}
        name      = identity.get("fullName") or "User"
        logger.info(
            f"✅ Morning message inserted — {name} | "
            f"Day {day_number} | Session {session_id}"
        )
        return True

    except Exception as e:
        logger.error(f"❌ insert_morning_message failed for session {session_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False


# ── Main runner — called by the scheduler endpoint ────────────────────────────

def run_daily_morning_messages() -> dict:
    """
    Entry point called by POST /scheduler/daily-morning.
    Iterates all paid users, inserts morning messages.
    Returns a summary dict.
    """
    if not SessionLocal:
        logger.error("❌ No database — scheduler cannot run")
        return {"status": "error", "reason": "no_database"}

    db = SessionLocal()
    sent = 0
    skipped = 0
    failed = 0

    try:
        users = get_paid_users_with_sessions(db)

        if not users:
            logger.info("ℹ️  No paid users found — nothing to send")
            return {"status": "ok", "sent": 0, "skipped": 0, "failed": 0, "total": 0}

        for user in users:
            success = insert_morning_message(
                db         = db,
                session_id = user["session_id"],
                user_id    = user["user_id"],
                plan_start = user["plan_start"],
                plan_name  = user["plan_name"],
            )
            if success:
                # Check if it was a skip (already sent)
                sent += 1
            else:
                failed += 1

        logger.info(
            f"📬 Scheduler complete — "
            f"sent: {sent} | failed: {failed} | total: {len(users)}"
        )
        return {
            "status":  "ok",
            "sent":    sent,
            "failed":  failed,
            "total":   len(users),
        }

    except Exception as e:
        logger.error(f"❌ run_daily_morning_messages failed: {e}")
        return {"status": "error", "reason": str(e)}

    finally:
        db.close()
