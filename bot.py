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
from typing import Dict, Any, List, Tuple

import gdown
import requests
import pypdf

# Dual SDK Support: Modern google-genai + google.generativeai
try:
    from google import genai as new_genai
    from google.genai import types as new_genai_types
    HAS_NEW_GENAI = True
except ImportError:
    HAS_NEW_GENAI = False

import google.generativeai as legacy_genai

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
    PollAnswerHandler,
    ContextTypes,
    filters,
)

# Global Poll ID to Session Mapping
POLL_LOOKUP: Dict[str, Dict[str, Any]] = {}

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

# Initialize Gemini Client (Supports both AQ... and AIzaSy... keys)
genai_client = None
if GEMINI_API_KEY:
    if HAS_NEW_GENAI:
        try:
            genai_client = new_genai.Client(api_key=GEMINI_API_KEY)
            logger.info("Initialized modern google-genai client.")
        except Exception as e:
            logger.warning(f"Could not init modern genai client: {e}")
    
    try:
        legacy_genai.configure(api_key=GEMINI_API_KEY)
        legacy_model = legacy_genai.GenerativeModel("gemini-1.5-flash")
        logger.info("Configured legacy google.generativeai.")
    except Exception as e:
        logger.warning(f"Legacy genai init error: {e}")
        legacy_model = None
else:
    logger.warning("GEMINI_API_KEY is not set!")
    legacy_model = None

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
    # If SELF_PING_URL is a placeholder, fallback to local internal port
    target = SELF_PING_URL.rstrip("/") + "/" if SELF_PING_URL and "your-app-name" not in SELF_PING_URL else f"http://127.0.0.1:{PORT}/"
    logger.info(f"Keep-alive monitor targeting: {target}")

    while True:
        try:
            time.sleep(500)
            req = urllib.request.Request(target, headers={"User-Agent": "RenderSelfPinger/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    logger.info("Keep-alive ping success (200 OK).")
        except Exception as e:
            logger.debug(f"Keep-alive ping debug: {e}")


# ----------------- ROBUST GOOGLE DRIVE & WEB DOWNLOADER -----------------
def download_large_file(url: str, output_path: str) -> bool:
    if "drive.google.com" in url or "drive.usercontent.google.com" in url:
        drive_match = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"id=([a-zA-Z0-9_-]+)", url)
        file_id = drive_match.group(1) if drive_match else None

        try:
            if file_id:
                res = gdown.download(id=file_id, output=output_path, quiet=False)
            else:
                res = gdown.download(url=url, output=output_path, quiet=False)
            if res and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                return True
        except Exception as e:
            logger.warning(f"gdown error: {e}, falling back to requests session...")

        if file_id:
            session = requests.Session()
            download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            response = session.get(download_url, stream=True, timeout=60)
            
            token = None
            for k, v in response.cookies.items():
                if k.startswith("download_warning"):
                    token = v
                    break
            
            if not token and "confirm=" in response.text:
                match = re.search(r"confirm=([a-zA-Z0-9_-]+)", response.text)
                if match:
                    token = match.group(1)

            if token:
                download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
                response = session.get(download_url, stream=True, timeout=120)

            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

            return os.path.exists(output_path) and os.path.getsize(output_path) > 10000

    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 10000


def clean_json_response(raw_text: str) -> List[Dict[str, Any]]:
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        text = text[start:end+1]

    return json.loads(text)


# ----------------- GEMINI QUESTION SYNTHESIS ENGINE -----------------
def generate_questions_with_gemini(file_path: str) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
    if not GEMINI_API_KEY:
        return {}, "GEMINI_API_KEY is not configured in Render environment."

    prompt = """
You are an advanced competitive exam question designer. Analyze this document completely (including Hindi and English text, current affairs, science, history, tables, and notes).
Identify the main chapters/topics.

CRITICAL INSTRUCTION:
For EVERY detected chapter/topic, generate between 25 to 50 comprehensive, high-yield Multiple Choice Questions (MINIMUM 25 questions, MAXIMUM 50 questions per section). Cover all key points, facts, and concepts in depth.

For each question provide:
- "topic": Topic / Chapter Name (in Hindi or English as in the document)
- "difficulty": "EASY" | "MEDIUM" | "HARD"
- "q_type": "CONCEPTUAL" | "CURRENT-AFFAIRS" | "STATEMENT-BASED"
- "question": High quality question text
- "options": Array of exactly 4 options ["A", "B", "C", "D"]
- "correct_index": Integer (0 for A, 1 for B, 2 for C, 3 for D)
- "concept": Core fact or theoretical principle
- "solution": In-depth step-by-step explanation
- "option_breakdown": Detailed analysis of options
- "pro_tip": Quick memory tip or key takeaway

Return ONLY a valid JSON array of objects.
"""

    errors_log = []

    # Dynamic model discovery + recommended target
    candidate_models = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.0-flash", "gemini-2.0-flash-exp", "gemini-1.5-flash-8b", "gemini-1.5-flash"]

    if genai_client:
        try:
            live_models = [m.name.replace("models/", "") for m in genai_client.models.list() if "generateContent" in str(getattr(m, "supported_generation_methods", []))]
            if live_models:
                candidate_models = live_models + [m for m in candidate_models if m not in live_models]
        except Exception as e:
            logger.warning(f"Could not fetch dynamic live models: {e}")

    # Method 1: Try modern google-genai SDK
    if genai_client:
        try:
            logger.info("Synthesizing questions using modern google-genai SDK...")
            uploaded = genai_client.files.upload(file=file_path)
            for _ in range(30):
                f_state = genai_client.files.get(name=uploaded.name)
                state_str = getattr(f_state, "state", "").upper() if hasattr(f_state, "state") else str(f_state.state)
                if "ACTIVE" in state_str:
                    break
                time.sleep(3)

            for m_name in candidate_models:
                try:
                    logger.info(f"Attempting generation with model: {m_name}")
                    config = None
                    if HAS_NEW_GENAI:
                        config = new_genai_types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.4
                        )
                    response = genai_client.models.generate_content(
                        model=m_name,
                        contents=[uploaded, prompt],
                        config=config
                    )
                    data = clean_json_response(response.text)
                    grouped: Dict[str, List[Dict[str, Any]]] = {}
                    for item in data:
                        t = item.get("topic", "General Section").strip()
                        if t not in grouped:
                            grouped[t] = []
                        grouped[t].append(item)
                    if grouped:
                        return grouped, ""
                except Exception as m_err:
                    logger.warning(f"Model {m_name} failed: {m_err}")
                    errors_log.append(f"Model {m_name}: {m_err}")

        except Exception as e:
            logger.warning(f"Modern genai error: {e}")
            errors_log.append(f"Modern SDK: {e}")

    # Method 2: Try legacy google.generativeai SDK
    if legacy_model:
        try:
            logger.info("Synthesizing questions using legacy google.generativeai SDK...")
            uploaded = legacy_genai.upload_file(file_path, mime_type="application/pdf")
            for _ in range(30):
                f_state = legacy_genai.get_file(uploaded.name)
                state_name = getattr(f_state.state, "name", str(f_state.state))
                if state_name == "ACTIVE":
                    break
                time.sleep(3)

            response = legacy_model.generate_content([uploaded, prompt])
            data = clean_json_response(response.text)
            grouped = {}
            for item in data:
                t = item.get("topic", "General Section").strip()
                if t not in grouped:
                    grouped[t] = []
                grouped[t].append(item)
            if grouped:
                return grouped, ""
        except Exception as e:
            logger.warning(f"Legacy genai error: {e}")
            errors_log.append(f"Legacy SDK: {e}")

    # Method 3: Local Text Page Chunking Fallback
    try:
        logger.info("Executing text chunk extraction fallback...")
        reader = pypdf.PdfReader(file_path)
        extracted = []
        for page in reader.pages[:40]:
            txt = page.extract_text()
            if txt:
                extracted.append(txt)
        raw_text = "\n".join(extracted)[:35000]

        if raw_text.strip():
            fallback_prompt = prompt + f"\n\nContent:\n{raw_text}"
            for m_name in candidate_models:
                try:
                    if genai_client:
                        resp = genai_client.models.generate_content(
                            model=m_name,
                            contents=fallback_prompt
                        )
                    elif legacy_model:
                        resp = legacy_model.generate_content(fallback_prompt)
                    else:
                        resp = None

                    if resp:
                        data = clean_json_response(resp.text)
                        grouped = {}
                        for item in data:
                            t = item.get("topic", "General Section").strip()
                            if t not in grouped:
                                grouped[t] = []
                            grouped[t].append(item)
                        if grouped:
                            return grouped, ""
                except Exception as fb_m_err:
                    logger.warning(f"Fallback model {m_name} failed: {fb_m_err}")
    except Exception as e:
        logger.error(f"Fallback extraction error: {e}")
        errors_log.append(f"Text Fallback: {e}")

    detailed_err = "\n".join(errors_log) if errors_log else "Authentication failed."
    return {}, detailed_err


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
        "📥 *How to Ingest Material:*\n"
        "1. **Direct Upload**: Send any `.pdf` document up to 20 MB directly.\n"
        "2. **Google Drive Link (Any Size: 100MB+, 500MB, Full Books)**: Paste the Google Drive link directly here!\n\n"
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

        status = await update.message.reply_text("⏳ *Step 1/3:* Fetching document from Google Drive...", parse_mode="Markdown")
        
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        loop = asyncio.get_running_loop()
        try:
            success = await loop.run_in_executor(None, download_large_file, text, tmp_path)
            if not success or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 1000:
                raise Exception("Downloaded file is empty or permission denied.")
        except Exception as e:
            logger.error(f"Download failure: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            await status.edit_text(
                f"❌ *Download Failed:* `{e}`\n\n"
                f"👉 Verify that Google Drive sharing is set to **'Anyone with the link'** (Viewer) and try again.",
                parse_mode="Markdown"
            )
            return

        file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        await status.edit_text(f"✅ *Downloaded:* `{file_size_mb:.1f} MB`\n⏳ *Step 2/3:* Uploading to Gemini AI & generating chapter MCQs...", parse_mode="Markdown")
        await process_and_prompt_topics(update, context, status, tmp_path)


async def process_and_prompt_topics(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg, file_path: str):
    user_id = update.effective_user.id

    loop = asyncio.get_running_loop()
    grouped, error_msg = await loop.run_in_executor(None, generate_questions_with_gemini, file_path)

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception:
            pass

    if not grouped:
        await status_msg.edit_text(
            f"❌ *AI Question Generation Failed*\n\n"
            f"**Details:** `{error_msg}`\n\n"
            f"👉 Make sure GEMINI_API_KEY is saved in your Render Environment Variables.",
            parse_mode="Markdown"
        )
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
        f"🎯 *Document Analyzed!* Found {len(topics)} chapters/sections:\nChoose which topic to launch in your group:",
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
            f"📋 *Questions:* `{len(mcqs)}`\n"
            f"⏱️ *Timer:* `15s per question`\n"
            f"✅ *Correct mark:* `+1.0`\n"
            f"➖ *Negative:* `None`\n"
            f"🔀 *Shuffle Q:* ❌ | *Options:* ❌\n"
            f"💡 *Show explanation:* ✅ Yes\n"
            f"🛡️ *Anti-Cheat:* ❌ Off\n"
            f"📢 *Promo messages enabled*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🚀 *Starting now — good luck!*"
        )
        await context.bot.send_message(chat_id=target_group, text=intro_card, parse_mode="Markdown")
        group_chat_id = (await context.bot.get_chat(target_group)).id
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to reach `{target_group}`: `{e}`\nCheck bot admin permissions and try again:")
        return

    state["step"] = "LAUNCHED"
    await update.message.reply_text(f"🔥 *Quiz initiated in* `{target_group}`!", parse_mode="Markdown")

    ACTIVE_GROUP_QUIZZES[str(group_chat_id)] = {
        "mcqs": mcqs,
        "current_index": 0,
        "topic": topic,
        "scores": {}
    }

    asyncio.create_task(run_native_quiz_polls(group_chat_id, context))


# ----------------- NATIVE TELEGRAM QUIZ POLL ENGINE -----------------
async def run_native_quiz_polls(group_chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    chat_key = str(group_chat_id)
    session = ACTIVE_GROUP_QUIZZES.get(chat_key)
    if not session:
        return

    mcqs = session["mcqs"]
    total_q = len(mcqs)

    for idx, item in enumerate(mcqs):
        session["current_index"] = idx

        # Format question: [1/76] Question text here
        raw_q = item.get("question", "").strip()
        q_title = f"[{idx + 1}/{total_q}] {raw_q}"
        if len(q_title) > 295:
            q_title = q_title[:292] + "..."

        # Truncate options to Telegram 100 character limit
        options = [str(opt)[:98] for opt in item.get("options", [])]

        # Explanation (pops up via lightbulb 💡 icon on answer)
        explanation = item.get("solution", "") or item.get("concept", "")
        if len(explanation) > 195:
            explanation = explanation[:192] + "..."

        correct_id = int(item.get("correct_index", 0))
        if correct_id < 0 or correct_id >= len(options):
            correct_id = 0

        try:
            poll_msg = await context.bot.send_poll(
                chat_id=group_chat_id,
                question=q_title,
                options=options,
                type="quiz",
                correct_option_id=correct_id,
                explanation=explanation,
                is_anonymous=False,
                open_period=15  # 15s visual countdown timer ring
            )

            # Store mapping for user score tracking
            POLL_LOOKUP[poll_msg.poll.id] = {
                "group_chat_id": group_chat_id,
                "correct_option_id": correct_id
            }

        except Exception as poll_err:
            logger.error(f"Poll send failed: {poll_err}")

        # Wait 15s timer + 2s intermission before next question
        await asyncio.sleep(17)

    # ----------------- FINAL QUIZ SUMMARY -----------------
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
        leaderboard_text += "_Session completed._\n"

    leaderboard_text += "\n🎉 *Good job everyone! Upload new material in DM to run next quiz.*"

    await context.bot.send_message(
        chat_id=group_chat_id,
        text=leaderboard_text,
        parse_mode="Markdown"
    )
    ACTIVE_GROUP_QUIZZES.pop(chat_key, None)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks live participant votes and scores from Native Quiz Polls."""
    answer = update.poll_answer
    poll_id = answer.poll_id
    user = answer.user
    selected = answer.option_ids[0] if answer.option_ids else -1

    if poll_id in POLL_LOOKUP:
        info = POLL_LOOKUP[poll_id]
        chat_key = str(info["group_chat_id"])
        session = ACTIVE_GROUP_QUIZZES.get(chat_key)
        if session:
            user_id = user.id
            if user_id not in session["scores"]:
                session["scores"][user_id] = {
                    "name": user.first_name,
                    "correct": 0,
                    "total": 0
                }
            session["scores"][user_id]["total"] += 1
            if selected == info["correct_option_id"]:
                session["scores"][user_id]["correct"] += 1


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
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    logger.info("Native QuizBot engine is active.")
    app.run_polling(drop_pending_updates=True, poll_interval=1.0, timeout=30)


if __name__ == "__main__":
    main()
