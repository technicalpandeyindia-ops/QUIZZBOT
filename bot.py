# language: Python, file: bot.py, runtime: Python 3.10+, target: Render / Telegram Bot API
import os
import io
import time
import json
import asyncio
import logging
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List

import pypdf
import google.generativeai as genai
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
PORT = int(os.getenv("PORT", 8080))
SELF_PING_URL = os.getenv("SELF_PING_URL", os.getenv("RENDER_EXTERNAL_URL", ""))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

START_TIME = time.time()

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# Session state containers
ADMIN_STATE: Dict[int, Dict[str, Any]] = {}
ACTIVE_GROUP_QUIZZES: Dict[str, Dict[str, Any]] = {}


# ----------------- 24/7 UPTIME ROBOT & HEALTH SERVER -----------------
class UptimeHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        uptime_seconds = int(time.time() - START_TIME)
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{days}d {hours}h {mins}m {secs}s"

        response_data = {
            "status": "online",
            "uptime": uptime_str,
            "uptime_seconds": uptime_seconds,
            "active_group_quizzes": len(ACTIVE_GROUP_QUIZZES)
        }

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response_data).encode("utf-8"))

    def log_message(self, format, *args):
        return


def start_uptime_server():
    server = HTTPServer(("0.0.0.0", PORT), UptimeHealthHandler)
    logger.info(f"Uptime HTTP server running on port {PORT}")
    server.serve_forever()


def start_self_pinger():
    if not SELF_PING_URL:
        logger.info("No SELF_PING_URL specified. Relying on external UptimeRobot.")
        return

    target = SELF_PING_URL.rstrip("/") + "/"
    logger.info(f"Self-pinger active targeting: {target}")

    while True:
        try:
            time.sleep(500)
            req = urllib.request.Request(target, headers={"User-Agent": "RenderSelfPinger/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("Self-ping success. Instance awake.")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")


# ----------------- MODERN GEMINI QUESTION & SOLUTION GENERATOR -----------------
def extract_text_from_pdf(file_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    pages_text = []
    for page in reader.pages:
        txt = page.extract_text()
        if txt:
            pages_text.append(txt)
    return "\n".join(pages_text)


def analyze_pdf_and_generate_modern_mcqs(raw_text: str) -> Dict[str, List[Dict[str, Any]]]:
    sample_text = raw_text[:40000]
    prompt = f"""
You are an advanced exam design intelligence. Analyze the document, divide it into distinct chapters/topics, and synthesize modern-style, competitive exam questions (including Assertion-Reason, Statement Evaluation, Case Scenarios, and Deep Conceptual MCQs).

For each question provide:
1. "topic": Precise Chapter/Topic Name
2. "difficulty": "EASY" | "MEDIUM" | "HARD"
3. "q_type": "CONCEPTUAL" | "ASSERTION-REASON" | "STATEMENT-BASED" | "APPLICATION"
4. "question": High quality, structured question text
5. "options": Exactly 4 distinct options ["A text", "B text", "C text", "D text"]
6. "correct_index": 0, 1, 2, or 3
7. "concept": Core theory or formula involved
8. "solution": Detailed step-by-step resolution
9. "option_breakdown": Why the correct option holds and why distractor options fail
10. "pro_tip": Trap alert, exam trick, or common student misconception

Return ONLY a JSON array matching this exact schema:
[
  {{
    "topic": "Topic Name",
    "difficulty": "HARD",
    "q_type": "STATEMENT-BASED",
    "question": "Consider the following statements...",
    "options": ["1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2"],
    "correct_index": 2,
    "concept": "Fundamental principle description",
    "solution": "Step-by-step logical proof and mathematical derivation.",
    "option_breakdown": "Option A misses factor X. Option B ignores constraint Y. Option C satisfies both conditions.",
    "pro_tip": "Look out for absolute qualifiers like 'always' or 'never' in statement 1."
  }}
]

Content:
{sample_text}
"""
    response = gemini_model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"}
    )
    try:
        data = json.loads(response.text)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in data:
            topic_name = item.get("topic", "General Section").strip()
            if topic_name not in grouped:
                grouped[topic_name] = []
            grouped[topic_name].append(item)
        return grouped
    except Exception as e:
        logger.error(f"Gemini modern parse error: {e}")
        return {}


# ----------------- ADMIN INTERFACE (PRIVATE DM) -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.effective_chat.type != "private":
        await update.message.reply_text("👋 Bot is active. Admin runs setups in private DM.")
        return

    is_admin = (user_id == ADMIN_USER_ID or ADMIN_USER_ID == 0)
    if not is_admin:
        await update.message.reply_text("👋 Hello! Tests will run in your configured community channel/group.")
        return

    await update.message.reply_text(
        "⚡ *AI-Powered Modern Exam Engine*\n\n"
        "1. Send your `.pdf` syllabus / book / study material.\n"
        "2. Gemini extracts topics & generates modern-style questions (Assertion-Reason, Statement-based, Conceptual).\n"
        "3. Choose your target chapter & group username.\n"
        "4. Live quiz runs with instant feedback, live leaderboards, and detailed analytical solutions.",
        parse_mode="Markdown"
    )


async def handle_admin_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Please send a valid `.pdf` file.")
        return

    status = await update.message.reply_text("⏳ *Step 1/3:* Ingesting PDF & reading document stream...", parse_mode="Markdown")
    file_obj = await context.bot.get_file(doc.file_id)
    pdf_bytes = await file_obj.download_as_bytearray()

    await status.edit_text("🧠 *Step 2/3:* Gemini AI generating modern competitive exam questions & deep solutions...", parse_mode="Markdown")
    loop = asyncio.get_running_loop()
    raw_text = await loop.run_in_executor(None, extract_text_from_pdf, bytes(pdf_bytes))

    if not raw_text.strip():
        await status.edit_text("❌ Could not extract text from document.")
        return

    grouped = await loop.run_in_executor(None, analyze_pdf_and_generate_modern_mcqs, raw_text)
    if not grouped:
        await status.edit_text("❌ Failed to synthesize questions from document.")
        return

    ADMIN_STATE[user_id] = {
        "step": "CHOOSE_TOPIC",
        "topics_data": grouped,
        "selected_topic": None
    }

    buttons = []
    topics = list(grouped.keys())
    for idx, t_name in enumerate(topics):
        count = len(grouped[t_name])
        buttons.append([InlineKeyboardButton(f"📂 {t_name} • [{count} Qs]", callback_data=f"adm_top:{idx}")])

    await status.edit_text(
        "🎯 *PDF Processed Successfully!*\nSelect the chapter/topic to launch:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


async def handle_admin_topic_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    state = ADMIN_STATE.get(user_id)
    if not state or "topics_data" not in state:
        await query.edit_message_text("❌ Session expired. Re-upload document.")
        return

    topic_idx = int(query.data.split(":")[1])
    topics = list(state["topics_data"].keys())
    if topic_idx >= len(topics):
        return

    chosen_topic = topics[topic_idx]
    state["selected_topic"] = chosen_topic
    state["step"] = "AWAITING_GROUP"

    await query.edit_message_text(
        f"🏷️ *Selected Topic:* `{chosen_topic}`\n\n"
        f"📢 *Send target group username or ID:*\n"
        f"Examples: `@mygroup` or `-1001234567890`",
        parse_mode="Markdown"
    )


async def handle_admin_group_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    state = ADMIN_STATE.get(user_id)
    if not state or state.get("step") != "AWAITING_GROUP":
        return

    target_group = update.message.text.strip()
    topic = state["selected_topic"]
    mcqs = state["topics_data"][topic]

    try:
        intro_card = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 *LIVE ARENA: MODERN MCQ TEST*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📖 *Topic:* `{topic}`\n"
            f"📊 *Questions:* `{len(mcqs)}`\n"
            f"⏱️ *Timer:* 5 seconds to lock answer\n"
            f"💡 *Format:* Assertion, Statement & Conceptual MCQs\n"
            f"🏆 *Live Member Leaderboard at completion*\n\n"
            f"👉 _Get ready. First question incoming..._"
        )
        sent = await context.bot.send_message(chat_id=target_group, text=intro_card, parse_mode="Markdown")
        group_chat_id = sent.chat.id
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to reach `{target_group}`: `{e}`\nCheck bot permissions and try again:")
        return

    state["step"] = "LAUNCHED"
    await update.message.reply_text(f"🔥 *Quiz initiated in* `{target_group}`!", parse_mode="Markdown")

    ACTIVE_GROUP_QUIZZES[str(group_chat_id)] = {
        "mcqs": mcqs,
        "current_index": 0,
        "topic": topic,
        "scores": {},       # { user_id: { "name": "...", "correct": 0, "total": 0 } }
        "answered_users": set()
    }

    asyncio.create_task(run_modern_group_quiz(group_chat_id, context))


# ----------------- MODERN GROUP QUIZ ENGINE -----------------
async def run_modern_group_quiz(group_chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    chat_key = str(group_chat_id)
    session = ACTIVE_GROUP_QUIZZES.get(chat_key)
    if not session:
        return

    mcqs = session["mcqs"]
    total_q = len(mcqs)

    for idx, item in enumerate(mcqs):
        session["current_index"] = idx
        session["answered_users"] = set()

        diff = item.get("difficulty", "MEDIUM").upper()
        q_type = item.get("q_type", "CONCEPTUAL").upper()
        diff_badge = {"EASY": "🟢 EASY", "MEDIUM": "🟡 MEDIUM", "HARD": "🔴 ADVANCED"}.get(diff, "🟡 MEDIUM")

        # Modern Question Card
        progress_bar = "█" * (idx + 1) + "░" * (total_q - idx - 1)
        q_card = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 *QUESTION {idx + 1}/{total_q}*  `[{progress_bar}]`\n"
            f"🏷️ `[{diff_badge}]` • `[{q_type}]`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"*{item['question']}*\n\n"
        )
        for opt_idx, opt in enumerate(item["options"]):
            letter = chr(65 + opt_idx)
            q_card += f"*{letter})* {opt}\n"

        buttons = []
        row = []
        for opt_idx in range(len(item["options"])):
            letter = chr(65 + opt_idx)
            cb = f"opt:{idx}:{opt_idx}"
            row.append(InlineKeyboardButton(f"👉 {letter}", callback_data=cb))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        keyboard = InlineKeyboardMarkup(buttons)
        question_message = await context.bot.send_message(
            chat_id=group_chat_id,
            text=q_card,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

        # 5-second countdown for member participation
        await asyncio.sleep(5)

        # Build Comprehensive Modern Solution & Analysis Card
        correct_idx = item["correct_index"]
        correct_letter = chr(65 + correct_idx)
        correct_text = item["options"][correct_idx]

        concept = item.get("concept", "Key theoretical principle.")
        solution = item.get("solution", "Step-by-step resolution.")
        breakdown = item.get("option_breakdown", "")
        pro_tip = item.get("pro_tip", "")

        solution_card = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *ANSWER KEY & DEEP ANALYSIS (Q{idx + 1})*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ *Correct Choice:* *{correct_letter}) {correct_text}*\n\n"
            f"🧠 *Core Concept:*\n_{concept}_\n\n"
            f"🔬 *Step-by-Step Solution:*\n{solution}\n\n"
        )
        if breakdown:
            solution_card += f"📊 *Option Elimination Analysis:*\n{breakdown}\n\n"
        if pro_tip:
            solution_card += f"💡 *Exam Pro-Tip / Trap Alert:*\n`{pro_tip}`\n\n"

        solution_card += f"⏳ _Next question in 5 seconds..._"

        try:
            await context.bot.send_message(
                chat_id=group_chat_id,
                text=solution_card,
                reply_to_message_id=question_message.message_id,
                parse_mode="Markdown"
            )
            await question_message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning(f"Error publishing solution: {e}")

        await asyncio.sleep(5)

    # ----------------- LEADERBOARD SUMMARY -----------------
    scores = session.get("scores", {})
    leaderboard_text = (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *TEST SUMMARY & LEADERBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 *Topic:* `{session['topic']}`\n"
        f"📊 *Total Questions:* `{total_q}`\n\n"
    )
    if scores:
        sorted_users = sorted(scores.values(), key=lambda x: x["correct"], reverse=True)
        medals = ["🥇", "🥈", "🥉", "🎖️", "🎖️"]
        for rank, u in enumerate(sorted_users[:10]):
            badge = medals[rank] if rank < len(medals) else "👤"
            pct = int((u["correct"] / total_q) * 100)
            leaderboard_text += f"{badge} *{u['name']}*: `{u['correct']}/{total_q}` ({pct}%)\n"
    else:
        leaderboard_text += "_No member votes recorded during this round._\n"

    leaderboard_text += "\n🎉 *Session concluded! Upload new material in DM to run next test.*"

    await context.bot.send_message(
        chat_id=group_chat_id,
        text=leaderboard_text,
        parse_mode="Markdown"
    )
    ACTIVE_GROUP_QUIZZES.pop(chat_key, None)


async def handle_user_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data

    if not data.startswith("opt:"):
        return

    _, q_idx_str, opt_idx_str = data.split(":")
    q_idx = int(q_idx_str)
    opt_idx = int(opt_idx_str)

    chat_key = str(update.effective_chat.id)
    session = ACTIVE_GROUP_QUIZZES.get(chat_key)
    if not session or session["current_index"] != q_idx:
        await query.answer("⌛ Time is up for this question!", show_alert=True)
        return

    # Check if user already answered this specific question
    user_id = user.id
    if user_id in session["answered_users"]:
        await query.answer("⚠️ You have already locked your answer for this question!", show_alert=True)
        return

    session["answered_users"].add(user_id)

    # Initialize user score tracking
    if user_id not in session["scores"]:
        session["scores"][user_id] = {
            "name": user.first_name,
            "correct": 0,
            "total": 0
        }
    session["scores"][user_id]["total"] += 1

    item = session["mcqs"][q_idx]
    correct_idx = item["correct_index"]

    # Real-time feedback alert
    if opt_idx == correct_idx:
        session["scores"][user_id]["correct"] += 1
        score_now = session["scores"][user_id]["correct"]
        feedback = f"🎯 BINGO, {user.first_name}! ✅\nChoice ({chr(65 + opt_idx)}) is CORRECT!\nCurrent Score: {score_now} pts."
    else:
        correct_letter = chr(65 + correct_idx)
        feedback = f"❌ INCORECT, {user.first_name}!\nYou chose ({chr(65 + opt_idx)}).\nCorrect is ({correct_letter}). See deep solution breakdown below."

    await query.answer(text=feedback, show_alert=True)


# ----------------- MAIN EXECUTION -----------------
def main():
    threading.Thread(target=start_uptime_server, daemon=True).start()
    threading.Thread(target=start_self_pinger, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_admin_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_admin_group_input))
    app.add_handler(CallbackQueryHandler(handle_admin_topic_choice, pattern=r"^adm_top:"))
    app.add_handler(CallbackQueryHandler(handle_user_selection, pattern=r"^opt:"))

    logger.info("Modern AI Quiz Bot listening.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
