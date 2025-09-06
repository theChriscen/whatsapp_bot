from fastapi import FastAPI, Form
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse
from sqlalchemy import create_engine, Column, String, Integer, Date, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime
import logging

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

app = FastAPI()

# --- Database Setup (SQLite) ---
DATABASE_URL = "sqlite:///./accountability.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# --- User Table ---
class User(Base):
    __tablename__ = "users"
    phone = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=True)
    goal = Column(String, nullable=True)
    points = Column(Integer, default=100)
    streak = Column(Integer, default=0)
    last_update = Column(Date, nullable=True)
    state = Column(String, default="idle")  # new, awaiting_name, awaiting_goal, active

    progress_entries = relationship("Progress", back_populates="user")


# --- Progress Table ---
class Progress(Base):
    __tablename__ = "progress"
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, ForeignKey("users.phone"))
    date = Column(Date, default=datetime.date.today)
    entry_text = Column(Text)

    user = relationship("User", back_populates="progress_entries")


Base.metadata.create_all(bind=engine)


# --- WhatsApp Webhook ---
@app.post("/whatsapp")
async def whatsapp_reply(From: str = Form(...), Body: str = Form(...)):
    logging.info(f"Incoming message from {From}: {Body}")
    db = SessionLocal()
    resp = MessagingResponse()
    msg = resp.message()
    text = Body.strip()
    lower_text = text.lower()

    try:
        # If first time user, register
        user = db.query(User).filter(User.phone == From).first()
        if not user:
            user = User(phone=From, points=100, streak=0, state="awaiting_name")
            db.add(user)
            db.commit()
            msg.body("👋 Welcome friend! I'm GAP - your goal accountability partner.\n\n"
                     "What's your name?")
            return Response(content=str(resp), media_type="application/xml")

        # If waiting for name
        if user.state == "awaiting_name":
            user.name = text
            user.state = "awaiting_goal"
            db.commit()
            msg.body(f"Nice to meet you, {user.name}! 🎉\n\n"
                     "What's the main goal you'd love to work on today?")
            return Response(content=str(resp), media_type="application/xml")

        # If waiting for goal
        if user.state == "awaiting_goal":
            user.goal = text
            user.state = "idle"
            db.commit()
            msg.body(f"✅ Got it, {user.name}! Your goal is to:\n{user.goal}\n\n"
                     "I'll check in with you later today. You can always log your progress by typing:\n"
                     "👉 'progress'\n\n"
                     "Want to see what else I can do? Type 'help'.")
            return Response(content=str(resp), media_type="application/xml")

        # If waiting for progress
        if user.state == "awaiting_progress":
            today = datetime.date.today()
            if user.last_update == today:
                msg.body("📊 You've already logged progress today. See you tomorrow!")
            else:
                user.streak += 1
                user.points += 100
                user.last_update = today

                new_entry = Progress(phone=user.phone, date=today, entry_text=text)
                db.add(new_entry)
                user.state = "idle"
                db.commit()
                msg.body(f"📈 Got it! Logged your progress 🎉\n"
                         f"Streak: {user.streak} days\n"
                         f"Points: {user.points}")
            return Response(content=str(resp), media_type="application/xml")

        # --- COMMANDS ---
        if "hello" in lower_text:
            msg.body(f"👋 Hey {user.name}, welcome back!\n"
                     "Hope you are having a productive day.\n\n"
                     "Type 'progress' to log today's progress "
                     "or 'help' to see all commands.")
            return Response(content=str(resp), media_type="application/xml")

        elif lower_text.startswith("goal"):
            goal_text = Body[5:].strip()
            if goal_text:
                user.goal = goal_text
                db.commit()
                msg.body(f"✅ Goal saved: {goal_text}")
            else:
                msg.body("Please enter a goal, e.g., 'goal read a book'")
            return Response(content=str(resp), media_type="application/xml")

        elif lower_text.startswith("progress"):
            today = datetime.date.today()
            entry_text = Body[9:].strip()

            if user.last_update == today:
                msg.body("📊 You've already reported progress today. See you tomorrow!")
            else:
                user.streak += 1
                user.points += 100
                user.last_update = today
                new_entry = Progress(phone=user.phone, date=today, entry_text=entry_text or "No progress shared!")
                db.add(new_entry)
                db.commit()
                msg.body(f"📈 Progress logged! 🎉\n"
                         f"Streak: {user.streak} days\n"
                         f"Points: {user.points}")
            return Response(content=str(resp), media_type="application/xml")

        elif "status" in lower_text:
            msg.body(f"📊 Your Status, {user.name}:\n"
                     f"Goal: {user.goal or 'Not set'}\n"
                     f"Streak: {user.streak} days\n"
                     f"Points: {user.points}")
            return Response(content=str(resp), media_type="application/xml")

        elif "history" in lower_text:
            entries = db.query(Progress).filter(Progress.phone == user.phone).order_by(Progress.date.desc()).limit(7).all()
            if not entries:
                msg.body("🗒 No history yet. Log progress.")
            else:
                history_text = "\n".join([f"{e.date}: {e.entry_text}" for e in entries])
                msg.body(f"🗒 Last 7 updates:\n{history_text}")
            return Response(content=str(resp), media_type="application/xml")

        elif "summary" in lower_text:
            today = datetime.date.today()
            last_7_days = today - datetime.timedelta(days=6)
            entries = db.query(Progress).filter(
                Progress.phone == user.phone,
                Progress.date >= last_7_days
            ).order_by(Progress.date).all()

            total_days = 7
            checkins = len(entries)
            percent = round((checkins / total_days) * 100, 1)

            if not entries:
                msg.body("📅 No progress in the last 7 days.")
            else:
                summary_text = "\n".join([f"{e.date}: ✅" for e in entries])
                msg.body(f"📅 Weekly Summary for {user.name}:\n"
                         f"{summary_text}\n\n"
                         f"Check-ins: {checkins}/{total_days} ({percent}%)\n"
                         f"Streak: {user.streak} days\n"
                         f"Points: {user.points}")
            return Response(content=str(resp), media_type="application/xml")

        elif "leaderboard" in lower_text:
            top_users = db.query(User).order_by(User.points.desc()).limit(10).all()
            if not top_users:
                msg.body("🏆 No leaderboard data yet")
            else:
                leaderboard_text = "\n".join(
                    [f"{i+1}. {u.phone[-4:]} | {u.points} pts | {u.streak}🔥"
                     for i, u in enumerate(top_users)]
                )
                msg.body(f"🏆 Leaderboard (Top 10):\n{leaderboard_text}")
            return Response(content=str(resp), media_type="application/xml")

        elif "withdraw" in lower_text:
            if user.streak >= 30:
                msg.body("💰 You're eligible for withdrawal! We'll process your points for cash.")
            else:
                msg.body(f"🚫 Not yet! You need 30 days streak. Current streak: {user.streak}")
            return Response(content=str(resp), media_type="application/xml")

        elif "help" in lower_text:
            msg.body("📝 Commands:\n"
                     "✅ goal - set your goal\n"
                     "📈 progress - log today's progress\n"
                     "📊 status - view your stats\n"
                     "🗒 history - last 7 updates\n"
                     "📅 summary - weekly summary\n"
                     "🏆 leaderboard - see active users\n"
                     "💰 withdraw - request cash\n"
                     "🤔 help - show this menu")
            return Response(content=str(resp), media_type="application/xml")

        else:
            msg.body("🤔 I didn't get that. Try asking for 'help'.")
            return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        logging.error(f"Error handling message: {e}")
        msg.body("⚠️ Oops! Something went wrong. Please try again later.")
        return Response(content=str(resp), media_type="application/xml")

    finally:
        db.close()