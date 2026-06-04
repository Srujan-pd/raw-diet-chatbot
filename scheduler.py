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


# ── Motivation line based on goal ─────────────────────────────────────────────

def get_motivation(goal: Optional[str], day_number: int) -> str:
    goal_lower = (goal or "").lower()

    if "loss" in goal_lower or "fat" in goal_lower or "slim" in goal_lower:
        lines = [
            "Every clean meal today is a step closer to your goal. You've got this! 🔥",
            "Consistency beats perfection. One good day at a time! 💪",
            "Your body is changing — trust the process and stay the course. 🌱",
            "Small choices today = big results tomorrow. Keep going! ⚡",
            "You didn't come this far to only come this far. Push through! 🏆",
        ]
    elif "gain" in goal_lower or "bulk" in goal_lower:
        lines = [
            "Fuel up well today — your muscles are counting on you! 💪",
            "Growth happens when you stay consistent with your nutrition. Keep eating right! 🏋️",
            "Every meal is a building block. Make them count today! 🧱",
            "Rest, eat, grow — that's the formula. You're doing great! 🌟",
            "Your gains are made in the kitchen first. Eat strong today! 🍽️",
        ]
    elif "muscle" in goal_lower or "strength" in goal_lower:
        lines = [
            "Strength is built one rep and one meal at a time. Today counts! 💥",
            "Protein up, stay hydrated, and give it everything today! 🥊",
            "Champions are built on days they don't feel like it. Show up! 🏆",
            "Your nutrition today is your performance tomorrow. Fuel wisely! ⚡",
            "Every gram of protein matters. Stick to your plan today! 💪",
        ]
    else:
        lines = [
            "A healthy day starts with the right mindset. You've got this! ��",
            "Nourish your body today — it's the only one you have! 🌿",
            "Small healthy habits, done consistently, change everything. Keep going! 🌱",
            "Eat well, move well, feel well. One good day today! ☀️",
            "Your health is your wealth. Invest in it today! 💚",
        ]

    return lines[(day_number - 1) % len(lines)]


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
        f"💧 *Water goal* — Drink {water_target} of water today\n"
        f"🍳 *Breakfast* — {meals['breakfast']}\n"
        f"🍽️ *Lunch* — {meals['lunch']}\n"
        f"🥗 *Evening snack* — {meals['snack']}\n"
        f"🌙 *Dinner* — {meals['dinner']}\n\n"
        f"💪 {motivation}\n\n"
        f"Tap here anytime if you have questions about your meals or need guidance. "
        f"I'm right here! 😊"
    )
    return message


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

        # Build message
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
