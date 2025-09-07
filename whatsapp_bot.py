# whatsapp_bot.py
import os
import re
import time
import logging
import datetime
from typing import Optional

from fastapi import FastAPI, Form, Response
from fastapi.responses import JSONResponse
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

from sqlalchemy import (
    create_engine, Column, String, Integer, Date, Text, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# ======================
# Logging
# ======================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("whatsapp-bot")

# ======================
# App
# ======================
app = FastAPI(title="GAP WhatsApp Bot")

# ======================
# Config / Env
# ======================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./accountability.db")

# Render/Postgres often requires SSL
if DATABASE_URL.startswith("postgresql") and "sslmode" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

# Twilio (optional for outbound messages like reminders)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")  # e.g. 'whatsapp:+14155238886'

# DB engine
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# ======================
# Models
# ======================
class User(Base):
    __tablename__ = "users"
    phone = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=True)
    goal = Column(String, nullable=True)
    points = Column(Integer, default=0)
    streak = Column(Integer, default=0)
    last_update = Column(Date, nullable=True)
    state = Column(String, default="idle")  # awaiting_name, awaiting_goal, awaiting_proof, idle
    reminder_time = Column(String, default="21:00")  # "HH:MM" in user's (assumed) UTC for now
    timezone = Column(String, default="UTC")  # placeholder

    progress_entries = relationship("Progress", back_populates="user", cascade="all, delete-orphan")

class Progress(Base):
    __tablename__ = "progress"
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, ForeignKey("users.phone"), index=True)
    date = Column(Date, default=datetime.date.today, index=True)
    entry_text = Column(Text, nullable=True)
    proof_url = Column(Text, nullable=True)  # image link or URL proof
    proof_status = Column(String, default="pending")  # pending | approved | rejected
    points_awarded = Column(Integer, default=0)  # 0 until approved

    user = relationship("User", back_populates="progress_entries")

Base.metadata.create_all(bind=engine)

# ======================
# Helpers
# ======================
URL_REGEX = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_REGEX.search(text)
    return m.group(1) if m else None

def today_utc() -> datetime.date:
    return datetime.datetime.utcnow().date()

def send_whatsapp(to: str, body: str) -> None:
    """Optional: send outbound WhatsApp via Twilio for reminders."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        logger.warning("Twilio env not fully set; skipping outbound send.")
        return
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to, body=body)
        logger.info(f"Sent WhatsApp to {to}: {body[:120]}...")
    except Exception as e:
        logger.exception(f"Failed to send WhatsApp to {to}: {e}")

def get_or_create_user(db, phone: str) -> User:
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        user = User(phone=phone, state="awaiting_name", reminder_time="21:00", points=0, streak=0)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

def get_pending_progress_for_today(db, phone: str) -> Optional[Progress]:
    return (
        db.query(Progress)
        .filter(
            Progress.phone == phone,
            Progress.date == today_utc(),
            Progress.proof_status == "pending"
        )
        .order_by(Progress.id.desc())
        .first()
    )

# ======================
# Health Check
# ======================
@app.get("/ping")
async def ping():
    logger.info("Ping check ✅")
    return JSONResponse({"status": "ok", "message": "GAP bot alive 🚀"})

# ======================
# WhatsApp Webhook
# ======================
@app.post("/whatsapp")
async def whatsapp_reply(
    From: str = Form(...),
    Body: str = Form(""),
    NumMedia: str = Form("0"),
    MediaUrl0: str = Form(None)
):
    start = time.time()
    db = SessionLocal()
    try:
        logger.info(f"Incoming from {From}: '{Body}' (NumMedia={NumMedia})")
        user = get_or_create_user(db, From)

        resp = MessagingResponse()
        msg = resp.message()

        text = (Body or "").strip()
        lower = text.lower()

        # ========= Conversation States =========

        # 1) Awaiting name
        if user.state == "awaiting_name":
            user.name = text if text else None
            user.state = "awaiting_goal"
            db.commit()
            reply = (
                f"Nice to meet you, {user.name or 'friend'}! 🎉\n\n"
                "What's the main goal you'd love to work on today?"
            )
            msg.body(reply)
            logger.info(f"Reply to {From}: {reply}")
            return Response(content=str(resp), media_type="application/xml")

        # 2) Awaiting goal
        if user.state == "awaiting_goal":
            if text:
                user.goal = text
                user.state = "idle"
                db.commit()
                reply = (
                    f"✅ Got it, {user.name or 'friend'}! Your goal is:\n{user.goal}\n\n"
                    "Log your progress anytime with:\n"
                    "👉 'progress I did X today'\n\n"
                    "After logging, send a proof (image or link) to earn points.\n"
                    "Type 'help' to see all commands."
                )
            else:
                reply = "Please enter your goal (just type it in)."
            msg.body(reply)
            logger.info(f"Reply to {From}: {reply}")
            return Response(content=str(resp), media_type="application/xml")

        # 3) Awaiting proof (image or link)
        if user.state == "awaiting_proof":
            # Look up today's pending progress
            pending = get_pending_progress_for_today(db, user.phone)
            if not pending:
                # No pending—reset state just in case
                user.state = "idle"
                db.commit()
                reply = "Hmm, I don’t see a pending progress entry for today. Try 'progress ...' again."
                msg.body(reply)
                logger.info(f"Reply to {From}: {reply}")
                return Response(content=str(resp), media_type="application/xml")

            proof_link = None

            # Image proof via Twilio Media*
            try:
                if NumMedia and int(NumMedia) > 0 and MediaUrl0:
                    proof_link = MediaUrl0
            except ValueError:
                pass

            # Or link proof in text
            if not proof_link:
                proof_link = extract_url(text)

            if not proof_link:
                reply = (
                    "I’m waiting for your proof. Please send an image or paste a link.\n"
                    "If you want to skip proof, reply 'skip' (no points will be awarded)."
                )
                msg.body(reply)
                logger.info(f"Reply to {From}: {reply}")
                return Response(content=str(resp), media_type="application/xml")

            # Auto-verify (simple accept)
            pending.proof_url = proof_link
            pending.proof_status = "approved"
            pending.points_awarded = 100
            user.points += 100
            user.state = "idle"
            db.commit()

            reply = (
                "🧾 Proof received and verified ✅\n"
                f"+100 points added. Total points: {user.points}\n"
                f"Streak: {user.streak} days\n"
                "Great job! 🎉"
            )
            msg.body(reply)
            logger.info(f"Reply to {From}: {reply}")
            return Response(content=str(resp), media_type="application/xml")

        # ========= Commands / Idle =========
        reply = None

        # help
        if "help" in lower:
            reply = (
                "📝 Commands:\n"
                "• goal <text> — set your goal\n"
                "• progress <what you did> — log today’s progress\n"
                "• status — see your stats\n"
                "• history — last 7 updates\n"
                "• summary — 7-day summary\n"
                "• leaderboard — top users\n"
                "• reminder <HH:MM> — set daily reminder time (UTC)\n"
                "• skip — skip proof (no points)\n"
            )

        # say hello
        elif "hello" in lower or "hi" in lower:
            reply = (
                f"👋 Hey {user.name or 'friend'}!\n"
                "Type 'progress <what you did>' to log today’s progress.\n"
                "Remember: send a proof (image or link) afterwards to earn points."
            )

        # set goal
        elif lower.startswith("goal"):
            goal_text = text[5:].strip()
            if goal_text:
                user.goal = goal_text
                db.commit()
                reply = f"✅ Goal saved:\n{goal_text}"
            else:
                reply = "Please enter a goal, e.g., 'goal Read 10 pages'."

        # log progress
        elif lower.startswith("progress"):
            entry_text = text[9:].strip()
            today = today_utc()

            if user.last_update == today:
                # already logged today
                # If they somehow are still idle, suggest proof if pending exists
                pending = get_pending_progress_for_today(db, user.phone)
                if pending:
                    user.state = "awaiting_proof"
                    db.commit()
                    reply = (
                        "You’ve already logged progress today. Please send proof (image or link) to earn points."
                    )
                else:
                    reply = "You’ve already reported progress today. See you tomorrow! 📊"
            else:
                # create progress & update streak, no points yet
                user.streak += 1
                user.last_update = today
                new_entry = Progress(
                    phone=user.phone,
                    date=today,
                    entry_text=entry_text or "No details provided."
                )
                db.add(new_entry)
                user.state = "awaiting_proof"
                db.commit()

                reply = (
                    "📈 Progress logged! 🎉\n"
                    f"Streak: {user.streak} days\n"
                    "Now send a proof (image or link). Points will be added after verification."
                )

        # skip proof (no points)
        elif lower.strip() == "skip":
            # clear pending if exists
            pending = get_pending_progress_for_today(db, user.phone)
            if pending:
                pending.proof_status = "rejected"
                user.state = "idle"
                db.commit()
                reply = (
                    "Okay, skipped proof for today. No points awarded.\n"
                    "You can still log progress tomorrow. 👍"
                )
            else:
                reply = "There’s no pending proof to skip today."

        # status
        elif "status" in lower:
            reply = (
                f"📊 Your Status, {user.name or 'friend'}:\n"
                f"Goal: {user.goal or 'Not set'}\n"
                f"Streak: {user.streak} days\n"
                f"Points: {user.points}\n"
                f"Reminder: {user.reminder_time or '21:00'} (UTC)"
            )

        # history
        elif "history" in lower:
            entries = (
                db.query(Progress)
                .filter(Progress.phone == user.phone)
                .order_by(Progress.date.desc())
                .limit(7)
                .all()
            )
            if not entries:
                reply = "🗒 No history yet. Use 'progress <text>' to log."
            else:
                lines = []
                for e in entries:
                    badge = "✅" if e.proof_status == "approved" else "⏳" if e.proof_status == "pending" else "❌"
                    lines.append(f"{e.date}: {e.entry_text} [{badge}]")
                reply = "🗒 Last 7 updates:\n" + "\n".join(lines)

        # summary
        elif "summary" in lower:
            today = today_utc()
            last_7 = today - datetime.timedelta(days=6)
            entries = (
                db.query(Progress)
                .filter(Progress.phone == user.phone, Progress.date >= last_7)
                .order_by(Progress.date)
                .all()
            )
            total_days = 7
            checkins = len(entries)
            percent = round((checkins / total_days) * 100, 1)
            if not entries:
                reply = "📅 No progress in the last 7 days."
            else:
                marks = "\n".join([f"{e.date}: {'✅' if e.proof_status=='approved' else '⏳'}" for e in entries])
                reply = (
                    f"📅 Weekly Summary for {user.name or 'you'}:\n"
                    f"{marks}\n\n"
                    f"Check-ins: {checkins}/{total_days} ({percent}%)\n"
                    f"Streak: {user.streak} days\n"
                    f"Points: {user.points}"
                )

        # leaderboard
        elif "leaderboard" in lower:
            top = db.query(User).order_by(User.points.desc()).limit(10).all()
            if not top:
                reply = "🏆 No leaderboard data yet."
            else:
                board = "\n".join(
                    f"{i+1}. {u.phone[-4:]} | {u.points} pts | {u.streak}🔥"
                    for i, u in enumerate(top)
                )
                reply = "🏆 Leaderboard (Top 10):\n" + board

        # set reminder time (UTC)
        elif lower.startswith("reminder"):
            # format: reminder HH:MM
            parts = text.split()
            if len(parts) >= 2 and re.match(r"^\d{2}:\d{2}$", parts[1]):
                user.reminder_time = parts[1]
                db.commit()
                reply = f"🕘 Reminder time set to {user.reminder_time} (UTC)."
            else:
                reply = "Use 'reminder HH:MM' in 24h format (UTC), e.g., 'reminder 21:00'."

        # image or link sent in idle? treat as late proof if pending exists
        elif (NumMedia and NumMedia.isdigit() and int(NumMedia) > 0) or extract_url(text):
            pending = get_pending_progress_for_today(db, user.phone)
            if pending:
                proof_link = MediaUrl0 if (NumMedia and int(NumMedia) > 0 and MediaUrl0) else extract_url(text)
                pending.proof_url = proof_link
                pending.proof_status = "approved"
                pending.points_awarded = 100
                user.points += 100
                user.state = "idle"
                db.commit()
                reply = (
                    "🧾 Proof received and verified ✅\n"
                    f"+100 points added. Total points: {user.points}\n"
                    f"Streak: {user.streak} days"
                )
            else:
                reply = "Thanks! I saved your message. If you meant this as proof, log progress first with 'progress ...'."

        # unknown
        else:
            reply = "🤔 I didn’t get that. Type 'help' to see what I can do."

        msg.body(reply)
        logger.info(f"Reply to {From}: {reply}")
        return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        logger.exception(f"Error handling message from {From}: {e}")
        # Return a TwiML apologizing so Twilio doesn’t retry in a loop.
        resp = MessagingResponse()
        resp.message("Whoops! Something broke on my end. Please try again in a minute. 🙏")
        return Response(content=str(resp), media_type="application/xml", status_code=200)
    finally:
        try:
            db.close()
        except Exception:
            pass
        logger.info(f"Processed request for {From} in {round((time.time()-start)*1000,2)} ms")

# ======================
# Simple cron endpoint for reminders
# Call this from a scheduler at minute 0 of every hour, for example.
# ======================
@app.post("/cron/remind")
async def cron_remind():
    """
    Very simple reminder pass:
      - If current UTC time matches user's reminder_time (HH:MM), send reminder.
      - This is intentionally basic; in production you may want a more robust scheduler.
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        logger.warning("Twilio env not set; /cron/remind will do nothing.")
        return JSONResponse({"ok": True, "sent": 0, "note": "Twilio not configured"})

    now = datetime.datetime.utcnow().strftime("%H:%M")
    db = SessionLocal()
    sent = 0
    try:
        users = db.query(User).all()
        for u in users:
            if (u.reminder_time or "21:00") == now:
                body = (
                    f"⏰ Reminder, {u.name or 'friend'}!\n"
                    f"Log your progress with 'progress ...' and send proof (image or link) to earn points."
                )
                send_whatsapp(u.phone, body)
                sent += 1
        logger.info(f"/cron/remind sent to {sent} users at {now} UTC")
        return JSONResponse({"ok": True, "sent": sent, "time": now})
    finally:
        db.close()
        