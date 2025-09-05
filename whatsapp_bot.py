from fastapi import FastAPI, Form
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse
from sqlalchemy import create_engine, Column, String, Integer, Date, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime

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
    goal = Column(String, nullable=True)
    points = Column(Integer, default=100)
    streak = Column(Integer, default=0)
    last_update = Column(Date, nullable=True)

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
    db = SessionLocal()
    user = db.query(User).filter(User.phone == From).first()

    # If first time user, register
    if not user:
        user = User(phone=From, points=100, streak=0)
        db.add(user)
        db.commit()

    resp = MessagingResponse()
    msg = resp.message()
    text = Body.strip().lower()

    if "hello" in text:
        msg.body("👋 Hi there, I'm Meka- you accountability partner!\n"
                 "Hope you are ready for the 100 Days of Reinvent Challenge!\n\n"
                 "Type any of the following commands to get started:\n"
                 "✅ goal \n"
                 "📈 progress \n"
                 "📊 status \n"
                 "🗒 history \n"
                 "📅 summary \n"
                 "🏆 leaderboard\n"
                 "💰 withdraw \n"
                 "🤔 help \n"                 
                 )
        
    elif text.startswith("goal"):
        goal_text = Body[5:].strip()
        if goal_text:
            user.goal = goal_text
            db.commit()
            msg.body(f"✅ Goal saved: {goal_text}")
        else:
            msg.body("Please enter a goal, e.g., 'read a book'")
    
    elif text.startswith("progress"):
        today = datetime.date.today()
        entry_text = Body[9:].strip()

        if user.last_update == today:
            msg.body("📊 You've already reported progress today. See you tomorrow!")
        else:
            user.streak += 1
            user.points += 100
            user.last_update = today

            # Save Progress entry
            new_entry = Progress(phone=user.phone, date=today, entry_text=entry_text or "No details given")
            db.add(new_entry)

            db.commit()
            msg.body(f"📈 Progress logged! 🎉\n"
                     f"Streak: {user.streak} days\n"
                     f"Points: {user.points}"
                     )
            
    elif "status" in text:
        msg.body(f"📊 Your Status:\n"
                 f"Goal: {user.goal or 'Not set'}\n"
                 f"Streak: {user.streak} days\n"
                 f"Points: {user.points}"
                 )
        
    elif "history" in text:
        entries = db.query(Progress).filter(Progress.phone == user.phone).order_by(Progress.date.desc()).limit(7).all()
        if not entries:
            msg.body("🗒  No history yet. Log progress")
        else:
            history_text = "\n".join([f"{e.date}: {e.entry_text}" for e in entries])
            msg.body(f"🗒 Last 7 updates:\n{history_text}")

    elif "summary" in text:
        today = datetime.date.today()
        last_7_days = today - datetime.timedelta(days=6)
        entries = db.query(Progress).filter(
            Progress.phone == user.phone,
            Progress.date >= last_7_days
        ).order_by(Progress.date).all()

        total_days = 7
        checkins = len(entries)
        percent = round((checkins/total_days) * 100, 1)

        if not entries:
            msg.body("📅 No progress in the last 7 days.")
        else:
            summary_text = "\n".join([f"{e.date}: ✅" for e in entries])
            msg.body(f"📅 Weekly Summary:\n"
                     f"{summary_text}\n\n"
                     f"Check-ins: {checkins}/{total_days} ({percent}%)\n"
                     f"Streak: {user.streak} days\n"
                     f"Points: {user.points}"                  
                     )

    elif "leaderboard" in text:
        top_users = db.query(User).order_by(User.points.desc()).limit(10).all()
        if not top_users:
            msg.data("🏆 No leaderboard data yet")
        else:
            leaderboard_text = "\n".join(
                [
                    f"{i+1}. {u.phone[-4:]} | {u.points} pts | {u.streak}🔥" for i, u in enumerate(top_users)
                ]
            )
            msg.body(f"🏆 Leaderboard (Top 10):\n{leaderboard_text}")

    elif "withdraw" in text:
        if user.streak >= 50:
            msg.body("💰 You're eligible for withdrawal! We'll process your points for cash.")
        else:
            msg.body(f"🚫 Not yet! You need 50 days streak. Current streak: {user.streak}")
    elif "help" in text:
        msg.body("📝 Commands: \n"
                 "hello -intro\n"
                 "progress -log today's progress\n"
                 "status - view your stats\n"
                 "history - view last 7 logs\n"
                 "summary - weekly progress\n"
                 "withdraw - request cash\n"
                 "help - show this menu"
                 )
    else:
        msg.body("🤔 I didn't get that. Try 'hello' or 'goal'." )
    
    db.close()
    return Response(content=str(resp), media_type="application/xml")