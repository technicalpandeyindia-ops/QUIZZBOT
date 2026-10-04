# language: Python, file: bot.py, runtime: Python 3.10+, target: Render / Telegram Bot API
import os
import io
import time
import json
import asyncio
import logging
import threading
import tempfile
import re
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List

import gdown
import requests
import pypdf
import google.generativeai as genai
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
PORT = int(os.getenv("PORT", 8080))
SELF_PING_URL = os.getenv("SELF_PING_URL", os.getenv("RENDER_EXTERNAL_URL", "")).strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

START_TIME = time.time()

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
else:
    logger.warning("GEMINI_API_KEY is not set!")
    gemini_model = None

# Session storage
ADMIN_STATE: Dict[int, Dict[str, Any]] = {}
ACTIVE_GROUP_QUIZZES: Dict[str, Dict[str, Any]] = {}


# ----------------- UPTIME KEEP-ALIVE SERVER -----------------
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
    server.serve_forever()


def start_self_pinger():
    if not SELF_PING_URL:
        return

    target = SELF_PING_URL.rstrip("/") + "/"
    while True:
        try:
            time.sleep(500)
            req = urllib.request.Request(target, headers={"User-Agent": "RenderSelfPinger/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    logger.info("Self-ping success.")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")


# ----------------- LARGE FILE DOWNLOADER (GOOGLE DRIVE & WEB) -----------------
def download_large_file(url: str, output_path: str) -> bool:
    """Handles large files including Google Drive virus scan bypass via gdown/requests."""
    if "drive.google.com" in url or "drive.usercontent.google.com" in url:
        logger.info(f"Downloading Google Drive file via gdown: {url}")
        res = gdown.download(url=url, output=output_path, quiet=False, fuzzy=True)
        if res and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True

        # Fallback to direct requests session with confirm token
        session = requests.Session()
        drive_match = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"id=([a-zA-Z0-9_-]+)", url)
        if drive_match:
            file_id = drive_match.group(1)
            download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            response = session.get(download_url, stream=True, timeout=60)
            for k, v in response.cookies.items():
                if k.startswith("download_warning"):
                    download_url = f"https://drive.google.com/uc?export=download&confirm={v}&id={file_id}"
                    response = session.get(download_url, stream=True, timeout=60)
                    break

            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            return os.path.exists(output_path) and os.path.getsize(output_path) > 1000

    # Direct Web URL download
    logger.info(f"Downloading direct URL: {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 1000


# ----------------- GEMINI FILE API PROCESSOR (UP TO 2GB) -----------------
def process_pdf_file_via_gemini(file_path: str) -> Dict[str, List[Dict[str, Any]]]:
    if not gemini_model:
        logger.error("Gemini model not initialized.")
        return {}

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    logger.info(f"Uploading file ({file_size_mb:.2f} MB) to Gemini File API...")

    uploaded_file = None
    try:
        uploaded_file = genai.upload_file(file_path, mime_type="application/pdf")
        
        # Wait until file processing completes on Gemini's backend
        for _ in range(30):
            file_info = genai.get_file(uploaded_file.name)
            state_name = getattr(file_info.state, "name", str(file_info.state))
            logger.info(f"Gemini file state: {state_name}")
            if state_name == "ACTIVE":
                break
            elif state_name == "FAILED":
                raise Exception("Gemini backend failed to parse PDF document.")
            time.sleep(3)

        prompt = """
You are an advanced exam design intelligence. Analyze this full book/document completely (including Hindi and English text, tables, and chapters).
Scan all major chapters/topics and synthesize high-quality modern competitive exam MCQs for each section.

For each question provide:
1. "topic": Specific Chapter or Topic Name (in the language of the document, e.g. Current Affairs, Science, History)
2. "difficulty": "EASY" | "MEDIUM" | "HARD"
3. "q_type": "CONCEPTUAL" | "ASSERTION-REASON" | "STATEMENT-BASED" | "CURRENT-AFFAIRS"
4. "question": High quality question in the same language as the book (Hindi or English)
5. "options": Exactly 4 distinct options ["Option A", "Option B", "Option C", "Option D"]
6. "correct_index": 0, 1, 2, or 3
7. "concept": Core theory or fact involved
8. "solution": Detailed step-by-step resolution and reasoning
9. "option_breakdown": Why correct choice is right and other choices are wrong
10. "pro_tip": Exam trap alert or key memory tip

Return ONLY a valid JSON array matching this exact schema:
[
  {
    "topic": "Chapter Name",
    "difficulty": "MEDIUM",
    "q_type": "CONCEPTUAL",
    "question": "Question text here",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correct_index": 0,
    "concept": "Underlying concept",
    "solution": "Detailed explanation",
    "option_breakdown": "Option analysis",
    "pro_tip": "Key memory tip"
  }
]
"""
        response = gemini_model.generate_content(
            [uploaded_file, prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        data = json.loads(response.text)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in data:
            t = item.get("topic", "General Section").strip()
            if t not in grouped:
                grouped[t] = []
            grouped[t].append(item)
        return grouped

    except Exception as e:
        logger.error(f"Gemini processing error: {e}")
        return {}
    finally:
        if uploaded_file:
            try:
                genai.delete_file(uploaded_file.name)
            except Exception:
                pass


# ----------------- ADMIN HANDLERS -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.effective_chat.type != "private":
        await update.message.reply_text("👋 Bot active. Admin commands run in private DM.")
        return

    is_admin = (user_id == ADMIN_USER_ID or ADMIN_USER_ID == 0)
    if not is_admin:
        await update.message.reply_text("👋 Hello! Tests will run in your configured community group.")
        return

    await update.message.reply_text(
        "⚡ *AI Exam & PDF Quiz Engine*\n\n"
        "📥 *Two Ways to Ingest Material:*\n"
        "1. **Direct Upload**: Send any `.pdf` document up to 20 MB directly.\n"
        "2. **Google Drive Link (Any Size: 100MB+, 500MB, Full Books)**: Paste the Google Drive share link directly here!\n\n"
        "👉 _Send a PDF file or paste your Google Drive link to start!_",
        parse_mode="Markdown"
    )


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Please send a valid `.pdf` file.")
        return

    file_size_mb = (doc.file_size or 0) / (1024 * 1024)

    if file_size_mb > 20.0:
        await update.message.reply_text(
            f"📦 *Large File Detected:* `{file_size_mb:.1f} MB`\n\n"
            f"Telegram API restricts bot downloads to 20 MB.\n\n"
            f"🚀 *How to process this entire {file_size_mb:.0f}MB book:*\n"
            f"1. Upload the PDF to your **Google Drive**.\n"
            f"2. Right click $\\to$ **Share** $\\to$ Set to *'Anyone with the link'*.\n"
            f"3. **Paste the Drive link right here in chat!**",
            parse_mode="Markdown"
        )
        return

    status = await update.message.reply_text(f"⏳ *Step 1/3:* Downloading PDF (`{file_size_mb:.1f} MB`)...", parse_mode="Markdown")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
        tmp_path = tmp_file.name

    try:
        file_obj = await context.bot.get_file(doc.file_id)
        await file_obj.download_to_drive(custom_path=tmp_path)
    except Exception as e:
        logger.error(f"Download failure: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        await status.edit_text(f"❌ *Download Error:* `{e}`")
        return

    await process_and_prompt_topics(update, context, status, tmp_path)


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    state = ADMIN_STATE.get(user_id, {})
    text = update.message.text.strip()

    if state.get("step") == "AWAITING_GROUP":
        await handle_admin_group_input(update, context)
        return

    if text.startswith("http://") or text.startswith("https://"):
        if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Unauthorized.")
            return

        status = await update.message.reply_text("⏳ *Step 1/3:* Fetching full document from Google Drive / Web...", parse_mode="Markdown")
        
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        loop = asyncio.get_running_loop()
        try:
            success = await loop.run_in_executor(None, download_large_file, text, tmp_path)
            if not success or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 1000:
                raise Exception("Downloaded file is empty or link requires permission.")
        except Exception as e:
            logger.error(f"URL download failure: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            await status.edit_text(
                f"❌ *Could not download from Google Drive:* `{e}`\n\n"
                f"👉 Make sure Google Drive sharing is set to **'Anyone with the link'** (Viewer) and try pasting the link again.",
                parse_mode="Markdown"
            )
            return

        file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        await status.edit_text(f"✅ *Document Downloaded:* `{file_size_mb:.1f} MB`\n⏳ *Step 2/3:* Uploading to Gemini AI & parsing Hindi/English chapters...", parse_mode="Markdown")
        await process_and_prompt_topics(update, context, status, tmp_path)


async def process_and_prompt_topics(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg, file_path: str):
    user_id = update.effective_user.id

    loop = asyncio.get_running_loop()
    try:
        grouped = await loop.run_in_executor(None, process_pdf_file_via_gemini, file_path)
    except Exception as e:
        logger.error(f"AI Synthesis error: {e}")
        grouped = {}
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

    if not grouped:
        await status_msg.edit_text("❌ *No questions could be synthesized.* Ensure the PDF contains readable text/scans and try again.")
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

    await status_msg.edit_text(
        f"🎯 *Full Document Analyzed!* Found {len(topics)} chapters/sections:\nChoose which topic to launch in your group:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


async def handle_admin_topic_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    state = ADMIN_STATE.get(user_id)
    if not state or "topics_data" not in state:
        await query.edit_message_text("❌ Session expired. Re-upload or paste link.")
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
        f"Examples: `@myquizgroup` or `-1001234567890`",
        parse_mode="Markdown"
    )


async def handle_admin_group_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"🚀 *LIVE ARENA: LIVE MCQ TEST*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📖 *Topic:* `{topic}`\n"
            f"📊 *Questions:* `{len(mcqs)}`\n"
            f"⏱️ *Timer:* 5 seconds to lock answer\n"
            f"💡 *Step-by-step solutions posted automatically*\n"
            f"🏆 *Live Leaderboard at completion*\n\n"
            f"👉 _First question incoming..._"
        )
        sent = await context.bot.send_message(chat_id=target_group, text=intro_card, parse_mode="Markdown")
        group_chat_id = sent.chat.id
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to reach `{target_group}`: `{e}`\nCheck bot admin permissions and try again:")
        return

    state["step"] = "LAUNCHED"
    await update.message.reply_text(f"🔥 *Quiz initiated in* `{target_group}`!", parse_mode="Markdown")

    ACTIVE_GROUP_QUIZZES[str(group_chat_id)] = {
        "mcqs": mcqs,
        "current_index": 0,
        "topic": topic,
        "scores": {},
        "answered_users": set()
    }

    asyncio.create_task(run_modern_group_quiz(group_chat_id, context))


# ----------------- GROUP QUIZ ENGINE -----------------
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

        await asyncio.sleep(5)

        correct_idx = item["correct_index"]
        correct_letter = chr(65 + correct_idx)
        correct_text = item["options"][correct_idx]

        concept = item.get("concept", "")
        solution = item.get("solution", "Step-by-step resolution.")
        breakdown = item.get("option_breakdown", "")
        pro_tip = item.get("pro_tip", "")

        solution_card = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *ANSWER KEY & DEEP ANALYSIS (Q{idx + 1})*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ *Correct Choice:* *{correct_letter}) {correct_text}*\n\n"
        )
        if concept:
            solution_card += f"🧠 *Concept:* _{concept}_\n\n"
        solution_card += f"🔬 *Explanation & Solution:*\n{solution}\n\n"
        if breakdown:
            solution_card += f"📊 *Option Elimination:*\n{breakdown}\n\n"
        if pro_tip:
            solution_card += f"💡 *Pro-Tip:*\n`{pro_tip}`\n\n"

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

    # Leaderboard Summary
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

    leaderboard_text += "\n🎉 *Session concluded!*"

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

    user_id = user.id
    if user_id in session["answered_users"]:
        await query.answer("⚠️ You have already answered this question!", show_alert=True)
        return

    session["answered_users"].add(user_id)

    if user_id not in session["scores"]:
        session["scores"][user_id] = {
            "name": user.first_name,
            "correct": 0,
            "total": 0
        }
    session["scores"][user_id]["total"] += 1

    item = session["mcqs"][q_idx]
    correct_idx = item["correct_index"]

    if opt_idx == correct_idx:
        session["scores"][user_id]["correct"] += 1
        score_now = session["scores"][user_id]["correct"]
        feedback = f"🎯 RIGHT ANSWER, {user.first_name}! ✅\n({chr(65 + opt_idx)}) is correct!\nScore: {score_now} pts."
    else:
        correct_letter = chr(65 + correct_idx)
        feedback = f"❌ WRONG, {user.first_name}!\nYou chose ({chr(65 + opt_idx)}).\nCorrect: ({correct_letter})."

    await query.answer(text=feedback, show_alert=True)


# ----------------- MAIN INITIALIZATION -----------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("FATAL: TELEGRAM_BOT_TOKEN missing!")
        return

    threading.Thread(target=start_uptime_server, daemon=True).start()
    threading.Thread(target=start_self_pinger, daemon=True).start()

    request_config = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0
    )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request_config)
        .get_updates_request(request_config)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_admin_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_admin_text))
    app.add_handler(CallbackQueryHandler(handle_admin_topic_choice, pattern=r"^adm_top:"))
    app.add_handler(CallbackQueryHandler(handle_user_selection, pattern=r"^opt:"))

    logger.info("Bot is active.")
    app.run_polling(drop_pending_updates=True, poll_interval=1.0, timeout=30)


if __name__ == "__main__":
    main()
