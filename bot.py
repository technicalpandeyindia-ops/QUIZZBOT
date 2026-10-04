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
import warnings
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List, Tuple

warnings.filterwarnings("ignore", category=FutureWarning)

import gdown
import requests
import pypdf

# Dual SDK Support: Modern google-genai + legacy fallback
try:
    from google import genai as new_genai
    from google.genai import types as new_genai_types
    HAS_NEW_GENAI = True
except ImportError:
    HAS_NEW_GENAI = False

try:
    import google.generativeai as legacy_genai
except ImportError:
    legacy_genai = None

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

# Initialize Gemini Client
genai_client = None
if GEMINI_API_KEY:
    if HAS_NEW_GENAI:
        try:
            genai_client = new_genai.Client(api_key=GEMINI_API_KEY)
            logger.info("Initialized modern google-genai client.")
        except Exception as e:
            logger.warning(f"Could not init modern genai client: {e}")
    
    if legacy_genai:
        try:
            legacy_genai.configure(api_key=GEMINI_API_KEY)
            legacy_model = legacy_genai.GenerativeModel("gemini-1.5-flash")
            logger.info("Configured legacy google.generativeai.")
        except Exception as e:
            logger.warning(f"Legacy genai init error: {e}")
            legacy_model = None
    else:
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


# ----------------- YOUTUBE TRANSCRIPT & CONTENT EXTRACTOR -----------------
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    HAS_YT_API = True
except ImportError:
    HAS_YT_API = False


def extract_youtube_video_id(url: str) -> str:
    match = re.search(r"(?:v=|\/live\/|\/shorts\/|youtu\.be\/|\/embed\/)([a-zA-Z0-9_-]{11})", url)
    return match.group(1) if match else ""


def get_youtube_video_content(url: str) -> Tuple[str, str, str]:
    """Extracts YouTube video title and transcript/captions."""
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return "", "", "Invalid YouTube URL. Please provide a valid YouTube video, live stream, or marathon link."

    title = f"YouTube Video ({video_id})"
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        res = requests.get(oembed_url, timeout=10)
        if res.status_code == 200:
            title = res.json().get("title", title)
    except Exception as e:
        logger.warning(f"oEmbed fetch error: {e}")

    transcript_text = ""
    if HAS_YT_API:
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['hi', 'en', 'hi-IN', 'en-IN'])
            transcript_text = " ".join([item['text'] for item in transcript_list])
        except Exception as yt_err:
            logger.warning(f"YouTubeTranscriptApi standard error: {yt_err}")
            try:
                transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
                for t in transcripts:
                    t_data = t.fetch()
                    transcript_text = " ".join([item['text'] for item in t_data])
                    if transcript_text:
                        break
            except Exception as e:
                logger.warning(f"Auto-transcript fetch error: {e}")

    return title, transcript_text, ""


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

    try:
        return json.loads(text)
    except Exception:
        fixed_text = re.sub(r",\s*([\]}])", r"\1", text)
        return json.loads(fixed_text)


# Global Model Cache
CACHED_WORKING_MODELS: List[str] = []
PRIMARY_WORKING_MODEL: str = ""


def get_available_gemini_models() -> List[str]:
    global CACHED_WORKING_MODELS, PRIMARY_WORKING_MODEL
    if PRIMARY_WORKING_MODEL:
        return [PRIMARY_WORKING_MODEL] + [m for m in CACHED_WORKING_MODELS if m != PRIMARY_WORKING_MODEL]

    if CACHED_WORKING_MODELS:
        return CACHED_WORKING_MODELS

    valid = []
    if legacy_genai and GEMINI_API_KEY:
        try:
            for m in legacy_genai.list_models():
                methods = getattr(m, "supported_generation_methods", [])
                if "generateContent" in methods:
                    name = m.name.replace("models/", "")
                    valid.append(name)
        except Exception as e:
            logger.warning(f"Error querying legacy list_models: {e}")

    if not valid and genai_client:
        try:
            for m in genai_client.models.list():
                methods = getattr(m, "supported_generation_methods", [])
                if "generateContent" in str(methods):
                    name = m.name.replace("models/", "")
                    valid.append(name)
        except Exception as e:
            logger.warning(f"Error querying genai_client.models.list: {e}")

    if not valid:
        valid = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash-latest",
            "gemini-1.5-flash-002",
            "gemini-1.5-flash-001",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-2.0-flash-exp"
        ]
    CACHED_WORKING_MODELS = valid
    return valid


# ----------------- GEMINI PDF QUESTION SYNTHESIS ENGINE -----------------
def generate_questions_with_gemini(file_path: str) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
    global PRIMARY_WORKING_MODEL
    if not GEMINI_API_KEY:
        return {}, "GEMINI_API_KEY is not configured in Render environment."

    prompt = """
You are an advanced competitive exam question designer for UP Super TET, CTET, UPTET, and state teaching examinations.
Analyze this document completely.

CRITICAL INSTRUCTIONS & AUTHORITATIVE SOURCES:
1. Align questions with top YouTube Marathon sessions (Ashriti Institute @ASHRITIINSTITUTE, Edumantra Institute @EdumantraInstitute, Sachin Academy Mega Marathon, Himanshi Singh Let's Learn, Chandra Institute Allahabad, RWA Rojgar with Ankit, Utkarsh Classes, Exampur Teaching School, Testbook/Adda247) and leading coaching books (Youth Competition Times YCT Solved Papers, Ghatna Chakra पूर्वावलोकन, SCERT/NCERT, Kiran Publication, Drishti IAS, Arihant).
2. Generate between 25 to 50 comprehensive, high-yield Multiple Choice Questions for each topic.
3. RANDOMIZE CORRECT ANSWER POSITION: Distribute correct answers evenly across index 0 (A), 1 (B), 2 (C), and 3 (D).

For each question provide:
- "topic": Topic / Chapter Name
- "difficulty": "EASY" | "MEDIUM" | "HARD"
- "question": High quality question text (max 280 chars)
- "options": Exactly 4 options ["A", "B", "C", "D"] (each max 90 chars)
- "correct_index": Integer (0 for A, 1 for B, 2 for C, 3 for D) — randomly vary this!
- "concept": Core fact or theoretical principle
- "solution": In-depth step-by-step pedagogical explanation
- "pro_tip": Quick memory tip or key takeaway (max 180 chars)
- "source_ref": Authentic Reference (e.g. "Ashriti Institute Marathon & YCT Solved Papers", "Edumantra Institute / SCERT", "Ghatna Chakra पूर्वावलोकन & Sachin Academy", "Chandra Institute Allahabad PYQ", "RWA Super TET Series")

Return ONLY a valid JSON array of objects.
"""

    candidate_models = get_available_gemini_models()
    errors_log = []

    # 1. Legacy google.generativeai SDK
    if legacy_genai and GEMINI_API_KEY:
        try:
            logger.info("Uploading PDF to Gemini for analysis...")
            uploaded = legacy_genai.upload_file(file_path, mime_type="application/pdf")
            for _ in range(30):
                f_state = legacy_genai.get_file(uploaded.name)
                state_name = getattr(f_state.state, "name", str(f_state.state))
                if state_name == "ACTIVE":
                    break
                time.sleep(2)

            for m_name in candidate_models:
                try:
                    mod = legacy_genai.GenerativeModel(m_name)
                    response = mod.generate_content([uploaded, prompt])
                    data = clean_json_response(response.text)
                    grouped: Dict[str, List[Dict[str, Any]]] = {}
                    for item in data:
                        t = item.get("topic", "General Section").strip()
                        if t not in grouped:
                            grouped[t] = []
                        grouped[t].append(item)
                    if grouped:
                        PRIMARY_WORKING_MODEL = m_name
                        return grouped, ""
                except Exception as leg_m_err:
                    logger.warning(f"Legacy model {m_name} failed: {leg_m_err}")
                    errors_log.append(f"Legacy {m_name}: {leg_m_err}")
        except Exception as e:
            logger.warning(f"Legacy genai error: {e}")
            errors_log.append(f"Legacy SDK: {e}")

    # 2. Modern google-genai SDK
    if genai_client:
        try:
            uploaded = genai_client.files.upload(file=file_path)
            for _ in range(30):
                f_state = genai_client.files.get(name=uploaded.name)
                state_str = getattr(f_state, "state", "").upper() if hasattr(f_state, "state") else str(f_state.state)
                if "ACTIVE" in state_str:
                    break
                time.sleep(2)

            for m_name in candidate_models:
                try:
                    config = None
                    if HAS_NEW_GENAI:
                        config = new_genai_types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.3
                        )
                    response = genai_client.models.generate_content(
                        model=m_name,
                        contents=[uploaded, prompt],
                        config=config
                    )
                    data = clean_json_response(response.text)
                    grouped = {}
                    for item in data:
                        t = item.get("topic", "General Section").strip()
                        if t not in grouped:
                            grouped[t] = []
                        grouped[t].append(item)
                    if grouped:
                        PRIMARY_WORKING_MODEL = m_name
                        return grouped, ""
                except Exception as m_err:
                    logger.warning(f"Model {m_name} failed: {m_err}")
                    errors_log.append(f"{m_name}: {m_err}")

        except Exception as e:
            logger.warning(f"Modern genai error: {e}")
            errors_log.append(f"Modern SDK: {e}")

    # 3. Local Text Page Chunking Fallback
    try:
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
                    if legacy_genai:
                        mod = legacy_genai.GenerativeModel(m_name)
                        resp = mod.generate_content(fallback_prompt)
                    elif genai_client:
                        resp = genai_client.models.generate_content(
                            model=m_name,
                            contents=fallback_prompt
                        )
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
                            PRIMARY_WORKING_MODEL = m_name
                            return grouped, ""
                except Exception as fb_m_err:
                    logger.warning(f"Fallback model {m_name} failed: {fb_m_err}")
                    errors_log.append(f"Fallback {m_name}: {fb_m_err}")
    except Exception as e:
        logger.error(f"Fallback extraction error: {e}")
        errors_log.append(f"Extraction error: {e}")

    last_err_text = "\n".join(errors_log[-2:]) if errors_log else "Could not synthesize questions from PDF."
    return {}, last_err_text


# ----------------- HIGH-SPEED PARALLEL TOPIC GENERATOR -----------------
def generate_topic_batch(topic_name: str, batch_count: int, offset: int) -> Tuple[List[Dict[str, Any]], str]:
    global PRIMARY_WORKING_MODEL
    prompt = f"""
You are an elite competitive exam specialist for UP Super TET (उत्तर प्रदेश सुपर टीईटी), CTET, UPTET, and state teacher recruitment exams.
Topic: "{topic_name}"

CRITICAL INSTRUCTIONS & AUTHENTIC KNOWLEDGE SOURCES:
1. Synthesize questions aligned with top YouTube Marathon sessions:
   - Ashriti Institute (@ASHRITIINSTITUTE)
   - Edumantra Institute (@EdumantraInstitute)
   - Sachin Academy Mega Marathon (Sachin Chaudhary Sir)
   - Let's Learn (Himanshi Singh)
   - Chandra Institute Allahabad (Dinesh Sir / Chandra Team)
   - Rojgar with Ankit (RWA - Ankit Bhati Sir)
   - Utkarsh Classes (Kumar Gaurav Sir / Shikshak Team)
   - Exampur Teaching School (Vivek Sir)
   - Testbook SuperCoaching & Adda247 Teaching
2. Incorporate standard questions and trends from authoritative coaching publications:
   - Youth Competition Times (YCT) Chapterwise Solved Papers & Practice Sets
   - Ghatna Chakra पूर्वावलोकन (Purvavlokan)
   - NCERT & UP Basic Shiksha Parishad (SCERT) Textbooks (Classes 1-10)
   - Kiran Publication Teacher Recruitment Series
   - Drishti IAS / Drishti Shikshak Bharti Study Material
   - Arihant Master Guide & Solved Workbooks
3. Generate EXACTLY {batch_count} unique MCQs starting from #{offset + 1} in Hindi (or bilingual for English).
4. RANDOMIZE CORRECT ANSWER POSITION: Distribute correct answers evenly across index 0 (A), 1 (B), 2 (C), and 3 (D). Do NOT put the correct answer at index 0 (A) every time!
5. Format limits:
   - "question": Max 280 chars
   - "options": Exactly 4 distinct options, each MAX 90 chars
   - "correct_index": Integer (0 for A, 1 for B, 2 for C, 3 for D) — randomly vary this!
   - "concept": Core rule / theory
   - "solution": In-depth pedagogical solution & explanation
   - "pro_tip": Exam trick / memory mnemonic (max 180 chars)
   - "source_ref": Specific Reference Tag (e.g. "Ashriti Institute Marathon & YCT Solved Papers", "Edumantra Institute / SCERT Notes", "Sachin Academy Mega Marathon / YCT", "Ghatna Chakra पूर्वावलोकन & RWA Series", "Chandra Institute Allahabad PYQ", "Drishti IAS & Utkarsh Classes")

Return ONLY a valid JSON array of {batch_count} objects.
"""
    candidate_models = get_available_gemini_models()
    err_msgs = []

    # 1. Fast direct call with legacy google.generativeai SDK
    if legacy_genai and GEMINI_API_KEY:
        for m_name in candidate_models:
            try:
                mod = legacy_genai.GenerativeModel(m_name)
                resp = mod.generate_content(prompt)
                if resp and resp.text:
                    data = clean_json_response(resp.text)
                    if isinstance(data, list) and len(data) > 0:
                        PRIMARY_WORKING_MODEL = m_name
                        return data, ""
            except Exception as e:
                logger.warning(f"Legacy model {m_name} failed: {e}")
                err_msgs.append(f"Legacy {m_name}: {e}")

    # 2. Modern google-genai SDK
    if genai_client:
        for m_name in candidate_models:
            try:
                config = None
                if HAS_NEW_GENAI:
                    config = new_genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3
                    )
                resp = genai_client.models.generate_content(
                    model=m_name,
                    contents=prompt,
                    config=config
                )
                if resp and resp.text:
                    data = clean_json_response(resp.text)
                    if isinstance(data, list) and len(data) > 0:
                        PRIMARY_WORKING_MODEL = m_name
                        return data, ""
            except Exception as e:
                logger.warning(f"Modern model {m_name} failed: {e}")
                err_msgs.append(f"{m_name}: {e}")

    last_error = "\n".join(err_msgs[-2:]) if err_msgs else "No compatible Gemini model found or key invalid."
    return [], last_error


def generate_custom_topic_mcqs(topic_name: str, count: int) -> Tuple[List[Dict[str, Any]], str]:
    import concurrent.futures
    if not GEMINI_API_KEY:
        return [], "GEMINI_API_KEY is not configured in Render environment."

    count = max(5, min(count, 100))

    # Fast single batch for small counts
    if count <= 25:
        data, err = generate_topic_batch(topic_name, count, 0)
        return shuffle_mcq_options(data), err

    # High-speed parallel generation for larger counts (25-100 questions)
    batch_size = 25
    tasks = []
    offset = 0
    remaining = count

    while remaining > 0:
        c_size = min(remaining, batch_size)
        tasks.append((topic_name, c_size, offset))
        offset += c_size
        remaining -= c_size

    all_mcqs: List[Dict[str, Any]] = []
    last_err = ""

    # Execute all batches in parallel simultaneously (completes in 3-5 seconds!)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(generate_topic_batch, t[0], t[1], t[2]) for t in tasks]
        for future in concurrent.futures.as_completed(futures):
            try:
                batch_data, err = future.result()
                if batch_data:
                    all_mcqs.extend(batch_data)
                elif err:
                    last_err = err
            except Exception as ex:
                last_err = str(ex)

    if all_mcqs:
        return shuffle_mcq_options(all_mcqs[:count]), ""

    return [], last_err or "Could not synthesize questions for this topic. Verify API key and network."


# ----------------- HIGH-SPEED YOUTUBE VIDEO QUESTION GENERATOR -----------------
def generate_youtube_topic_batch(video_title: str, transcript_snippet: str, video_url: str, batch_count: int, offset: int) -> Tuple[List[Dict[str, Any]], str]:
    global PRIMARY_WORKING_MODEL
    prompt = f"""
You are an expert exam question creator for UP Super TET and competitive teacher examinations.
YouTube Class / Marathon Title: "{video_title}"
Video URL: "{video_url}"

Content / Transcript Excerpt from Video:
\"\"\"{transcript_snippet[:15000]}\"\"\"

CRITICAL INSTRUCTIONS:
1. Synthesize EXACTLY {batch_count} unique, high-yield Multiple Choice Questions starting from #{offset + 1} based on the key concepts, pedagogy, facts, rules, and questions taught in this YouTube class / marathon in Hindi (or bilingual for English).
2. Align with top exam patterns (UP Super TET, CTET, UPTET).
3. RANDOMIZE CORRECT ANSWER POSITION: Distribute correct answers evenly across index 0 (A), 1 (B), 2 (C), and 3 (D).
4. Format limits:
   - "question": Max 280 chars
   - "options": Exactly 4 distinct options, each MAX 90 chars
   - "correct_index": Integer (0 for A, 1 for B, 2 for C, 3 for D) — randomly vary this!
   - "concept": Core theoretical principle / rule
   - "solution": In-depth pedagogical solution & explanation
   - "pro_tip": Exam trick / memory mnemonic (max 180 chars)
   - "source_ref": Reference tag (e.g. "{video_title[:35]} • YouTube Marathon")

Return ONLY a valid JSON array of {batch_count} objects.
"""
    candidate_models = get_available_gemini_models()
    err_msgs = []

    if legacy_genai and GEMINI_API_KEY:
        for m_name in candidate_models:
            try:
                mod = legacy_genai.GenerativeModel(m_name)
                resp = mod.generate_content(prompt)
                if resp and resp.text:
                    data = clean_json_response(resp.text)
                    if isinstance(data, list) and len(data) > 0:
                        PRIMARY_WORKING_MODEL = m_name
                        return data, ""
            except Exception as e:
                logger.warning(f"Legacy model {m_name} failed on YT: {e}")
                err_msgs.append(f"Legacy {m_name}: {e}")

    if genai_client:
        for m_name in candidate_models:
            try:
                config = None
                if HAS_NEW_GENAI:
                    config = new_genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3
                    )
                resp = genai_client.models.generate_content(
                    model=m_name,
                    contents=prompt,
                    config=config
                )
                if resp and resp.text:
                    data = clean_json_response(resp.text)
                    if isinstance(data, list) and len(data) > 0:
                        PRIMARY_WORKING_MODEL = m_name
                        return data, ""
            except Exception as e:
                logger.warning(f"Modern model {m_name} failed on YT: {e}")
                err_msgs.append(f"{m_name}: {e}")

    last_error = "\n".join(err_msgs[-2:]) if err_msgs else "No compatible Gemini model found."
    return [], last_error


def generate_youtube_mcqs(video_title: str, transcript: str, video_url: str, count: int) -> Tuple[List[Dict[str, Any]], str]:
    import concurrent.futures
    if not GEMINI_API_KEY:
        return [], "GEMINI_API_KEY is not configured in Render environment."

    count = max(5, min(count, 100))

    if count <= 25:
        data, err = generate_youtube_topic_batch(video_title, transcript, video_url, count, 0)
        return shuffle_mcq_options(data), err

    batch_size = 25
    tasks = []
    offset = 0
    remaining = count

    while remaining > 0:
        c_size = min(remaining, batch_size)
        tasks.append((video_title, transcript, video_url, c_size, offset))
        offset += c_size
        remaining -= c_size

    all_mcqs: List[Dict[str, Any]] = []
    last_err = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(generate_youtube_topic_batch, t[0], t[1], t[2], t[3], t[4]) for t in tasks]
        for future in concurrent.futures.as_completed(futures):
            try:
                batch_data, err = future.result()
                if batch_data:
                    all_mcqs.extend(batch_data)
                elif err:
                    last_err = err
            except Exception as ex:
                last_err = str(ex)

    if all_mcqs:
        return shuffle_mcq_options(all_mcqs[:count]), ""

    return [], last_err or "Could not synthesize questions from YouTube video."


# ----------------- HIGH-SPEED FULL MODEL PAPER / MOCK TEST GENERATOR -----------------
def generate_model_paper_batch(paper_type: str, batch_count: int, offset: int) -> Tuple[List[Dict[str, Any]], str]:
    global PRIMARY_WORKING_MODEL
    prompt = f"""
You are the Chief Examiner for UP Super TET (उत्तर प्रदेश सुपर टीईटी) official competitive recruitment examination.
Task: Generate a Full Balanced Model Paper (मॉक टेस्ट / मॉडल पेपर).

CRITICAL INSTRUCTIONS & EXAM PATTERN:
1. Synthesize EXACTLY {batch_count} unique, high-yield Multiple Choice Questions starting from #{offset + 1} with balanced proportional representation across all UP Super TET sections:
   - बाल विकास एवं शिक्षण शास्त्र (CDP & Pedagogy)
   - हिंदी भाषा एवं व्याकरण (Hindi Grammar & Literature)
   - English Language & Grammar
   - संस्कृत भाषा एवं साहित्य (Sanskrit)
   - गणित एवं तार्किक ज्ञान (Mathematics & Reasoning)
   - पर्यावरण एवं सामाजिक अध्ययन (EVS & Social Studies)
   - दैनिक जीवन में विज्ञान (General Science)
   - समसामयिक घटनाएं एवं सामान्य ज्ञान (Current Affairs & GK)
   - सूचना तकनीकी एवं जीवन कौशल (IT, Computer & Life Skills)
2. Incorporate trends from top YouTube Marathons (Ashriti Institute, Edumantra Institute, Sachin Academy, Chandra Institute, RWA) and standard coaching books (YCT Solved Papers, Ghatna Chakra पूर्वावलोकन, SCERT/NCERT).
3. RANDOMIZE CORRECT ANSWER POSITION: Distribute correct answers evenly across index 0 (A), 1 (B), 2 (C), and 3 (D).
4. Format limits:
   - "question": Max 280 chars
   - "options": Exactly 4 distinct options, each MAX 90 chars
   - "correct_index": Integer (0 for A, 1 for B, 2 for C, 3 for D) — randomly vary this!
   - "concept": Core rule / theory
   - "solution": In-depth pedagogical solution & explanation
   - "pro_tip": Exam trick / memory mnemonic (max 180 chars)
   - "source_ref": Reference Tag (e.g. "UP Super TET Model Paper • YCT & Ashriti Marathon", "UP Super TET Official Pattern • Ghatna Chakra / SCERT")

Return ONLY a valid JSON array of {batch_count} objects.
"""
    candidate_models = get_available_gemini_models()
    err_msgs = []

    if legacy_genai and GEMINI_API_KEY:
        for m_name in candidate_models:
            try:
                mod = legacy_genai.GenerativeModel(m_name)
                resp = mod.generate_content(prompt)
                if resp and resp.text:
                    data = clean_json_response(resp.text)
                    if isinstance(data, list) and len(data) > 0:
                        PRIMARY_WORKING_MODEL = m_name
                        return data, ""
            except Exception as e:
                logger.warning(f"Legacy model {m_name} failed on Model Paper: {e}")
                err_msgs.append(f"Legacy {m_name}: {e}")

    if genai_client:
        for m_name in candidate_models:
            try:
                config = None
                if HAS_NEW_GENAI:
                    config = new_genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3
                    )
                resp = genai_client.models.generate_content(
                    model=m_name,
                    contents=prompt,
                    config=config
                )
                if resp and resp.text:
                    data = clean_json_response(resp.text)
                    if isinstance(data, list) and len(data) > 0:
                        PRIMARY_WORKING_MODEL = m_name
                        return data, ""
            except Exception as e:
                logger.warning(f"Modern model {m_name} failed on Model Paper: {e}")
                err_msgs.append(f"{m_name}: {e}")

    last_error = "\n".join(err_msgs[-2:]) if err_msgs else "No compatible Gemini model found."
    return [], last_error


def generate_full_model_paper_mcqs(count: int) -> Tuple[List[Dict[str, Any]], str]:
    import concurrent.futures
    if not GEMINI_API_KEY:
        return [], "GEMINI_API_KEY is not configured in Render environment."

    count = max(5, min(count, 100))

    if count <= 25:
        data, err = generate_model_paper_batch("UP Super TET Model Paper", count, 0)
        return shuffle_mcq_options(data), err

    batch_size = 25
    tasks = []
    offset = 0
    remaining = count

    while remaining > 0:
        c_size = min(remaining, batch_size)
        tasks.append(("UP Super TET Model Paper", c_size, offset))
        offset += c_size
        remaining -= c_size

    all_mcqs: List[Dict[str, Any]] = []
    last_err = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(generate_model_paper_batch, t[0], t[1], t[2]) for t in tasks]
        for future in concurrent.futures.as_completed(futures):
            try:
                batch_data, err = future.result()
                if batch_data:
                    all_mcqs.extend(batch_data)
                elif err:
                    last_err = err
            except Exception as ex:
                last_err = str(ex)

    if all_mcqs:
        return shuffle_mcq_options(all_mcqs[:count]), ""

    return [], last_err or "Could not synthesize Model Paper questions."


# ----------------- ADMIN HANDLERS -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.effective_chat.type != "private":
        await update.message.reply_text("👋 Bot active. Admin controls run in private DM.")
        return

    is_admin = (user_id == ADMIN_USER_ID or ADMIN_USER_ID == 0)
    if not is_admin:
        await update.message.reply_text("👋 Hello! Tests will run in your configured community group.")
        return

    buttons = [
        [InlineKeyboardButton("✍️ 1️⃣ Create by Topic (No PDF Needed)", callback_data="mode_topic")],
        [InlineKeyboardButton("📄 2️⃣ Upload PDF / Google Drive Link", callback_data="mode_pdf")],
        [InlineKeyboardButton("🎥 3️⃣ Create from YouTube Video / Marathon URL", callback_data="mode_yt")],
        [InlineKeyboardButton("🏆 4️⃣ Full Model Paper / Mock Test (मॉडल पेपर)", callback_data="mode_mock")]
    ]

    await update.message.reply_text(
        "⚡ *AI Exam & Quiz Master (UP Super TET & All Exams)*\n\n"
        "Choose how you want to create your quiz:\n\n"
        "1️⃣ *Create by Topic*: Pick any UP Super TET subject or custom topic.\n"
        "2️⃣ *PDF / Cloud Ingest*: Send any book PDF or Google Drive link.\n"
        "3️⃣ *YouTube Video / Marathon*: Paste any YouTube video or marathon class link.\n"
        "4️⃣ *Full Model Paper (मॉक टेस्ट)*: Launch a balanced full-syllabus simulation test!",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


UP_SUPERTET_SUBJECTS: Dict[str, str] = {
    "cdp": "बाल विकास एवं मनोविज्ञान (CDP)",
    "teach": "शिक्षण कौशल एवं शिक्षण विधियाँ",
    "life": "जीवन कौशल, प्रबंधन एवं अभिवृत्ति",
    "hindi": "हिंदी भाषा एवं व्याकरण",
    "evs": "पर्यावरण एवं सामाजिक अध्ययन (EVS & SST)",
    "sci": "दैनिक जीवन में विज्ञान (General Science)",
    "gk": "समसामयिक घटनाएं एवं सामान्य ज्ञान (GK & Current Affairs)",
    "math": "गणित एवं तार्किक ज्ञान (Mathematics & Reasoning)",
    "eng": "English Language & Comprehension",
    "sanskrit": "संस्कृत भाषा एवं व्याकरण",
    "it": "सूचना तकनीकी (Information Technology / Computer)",
    "up_gk": "भारतीय संविधान, शासन व्यवस्था एवं UP Special GK"
}


async def handle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    if data == "mode_topic":
        ADMIN_STATE[user_id] = {"step": "AWAITING_TOPIC_NAME"}
        
        subject_buttons = [
            [
                InlineKeyboardButton("👶 बाल विकास (CDP - 10M)", callback_data="sub_sel:cdp"),
                InlineKeyboardButton("📖 शिक्षण कौशल (10M)", callback_data="sub_sel:teach")
            ],
            [
                InlineKeyboardButton("⚖️ जीवन कौशल (10M)", callback_data="sub_sel:life"),
                InlineKeyboardButton("✍️ हिंदी भाषा (20M)", callback_data="sub_sel:hindi")
            ],
            [
                InlineKeyboardButton("🌍 EVS / सामाजिक अध्ययन (10M)", callback_data="sub_sel:evs"),
                InlineKeyboardButton("🔬 विज्ञान (10M)", callback_data="sub_sel:sci")
            ],
            [
                InlineKeyboardButton("📰 GK & Current Affairs (30M)", callback_data="sub_sel:gk"),
                InlineKeyboardButton("📐 गणित एवं रीजनिंग (25M)", callback_data="sub_sel:math")
            ],
            [
                InlineKeyboardButton("🔤 English Grammar (10M)", callback_data="sub_sel:eng"),
                InlineKeyboardButton("📜 संस्कृत भाषा (10M)", callback_data="sub_sel:sanskrit")
            ],
            [
                InlineKeyboardButton("💻 सूचना तकनीकी / IT (5M)", callback_data="sub_sel:it"),
                InlineKeyboardButton("🏛️ संविधान व UP GK", callback_data="sub_sel:up_gk")
            ]
        ]

        await query.edit_message_text(
            "📚 *UP Super TET 150-Marks All Subject Matrix:*\n\n"
            "👇 *Tap any subject below to generate instant exam quiz:*\n\n"
            "1️⃣ `बाल विकास (CDP)` • 10 Marks\n"
            "2️⃣ `शिक्षण कौशल (Teaching Methodology)` • 10 Marks\n"
            "3️⃣ `जीवन कौशल, प्रबंधन एवं अभिवृत्ति` • 10 Marks\n"
            "4️⃣ `हिंदी भाषा एवं व्याकरण` • 20 Marks\n"
            "5️⃣ `English Language & Grammar` • 10 Marks\n"
            "6️⃣ `संस्कृत भाषा एवं साहित्य` • 10 Marks\n"
            "7️⃣ `गणित एवं रीजनिंग (Maths & Logic)` • 25 Marks\n"
            "8️⃣ `पर्यावरण एवं सामाजिक अध्ययन (EVS & SST)` • 10 Marks\n"
            "9️⃣ `दैनिक जीवन में विज्ञान (Science)` • 10 Marks\n"
            "🔟 `समसामयिक घटनाएं एवं सामान्य ज्ञान (GK)` • 30 Marks\n"
            "1️⃣1️⃣ `सूचना तकनीकी (IT / Computer)` • 5 Marks\n"
            "1️⃣2️⃣ `भारतीय संविधान एवं UP Special GK`\n\n"
            "👉 *Tap a button above or type any custom topic name in chat:*",
            reply_markup=InlineKeyboardMarkup(subject_buttons),
            parse_mode="Markdown"
        )
    elif data == "mode_pdf":
        ADMIN_STATE[user_id] = {"step": "AWAITING_PDF"}
        await query.edit_message_text(
            "📄 *Send your PDF document or paste a Google Drive link right here in chat!*",
            parse_mode="Markdown"
        )
    elif data == "mode_yt":
        ADMIN_STATE[user_id] = {"step": "AWAITING_YT_URL"}
        await query.edit_message_text(
            "🎥 *Step 1/3: Send YouTube Video or Marathon URL*\n\n"
            "Paste any YouTube video link, live marathon, or practice set:\n"
            "• `https://www.youtube.com/watch?v=...`\n"
            "• `https://youtu.be/...`\n"
            "• `https://www.youtube.com/live/...`\n\n"
            "👉 *Paste your YouTube link here in chat:*",
            parse_mode="Markdown"
        )
    elif data == "mode_mock":
        mock_buttons = [
            [
                InlineKeyboardButton("⚡ 25 Qs Mini Model Paper", callback_data="mock_len:25"),
                InlineKeyboardButton("📊 50 Qs Standard Model Paper", callback_data="mock_len:50")
            ],
            [
                InlineKeyboardButton("🔥 75 Qs Mega Model Paper", callback_data="mock_len:75"),
                InlineKeyboardButton("👑 100 Qs Grand Model Paper", callback_data="mock_len:100")
            ],
            [
                InlineKeyboardButton("✍️ Custom Question Count", callback_data="mock_len:custom")
            ]
        ]
        await query.edit_message_text(
            "🏆 *UP Super TET Full Model Paper / Mock Test (मॉक टेस्ट)*\n\n"
            "Simulates the complete 150-mark balanced official exam pattern covering:\n"
            "• CDP & Teaching Skills • Hindi • English • Sanskrit\n"
            "• Maths & Reasoning • Science • EVS & SST • GK/Current Affairs • Life Skills & IT\n\n"
            "👇 *Select question count for this Model Paper:*",
            reply_markup=InlineKeyboardMarkup(mock_buttons),
            parse_mode="Markdown"
        )


async def handle_mock_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data
    val = data.split("mock_len:")[1]

    if val == "custom":
        ADMIN_STATE[user_id] = {"step": "AWAITING_MOCK_COUNT"}
        await query.edit_message_text(
            "🔢 *Enter Custom Question Count for Model Paper:*\n"
            "Examples: `15`, `30`, `45`, `60`, `90`\n\n"
            "👉 *Send the number of questions in chat:*",
            parse_mode="Markdown"
        )
    else:
        q_count = int(val)
        ADMIN_STATE[user_id] = {
            "mock_q_count": q_count,
            "step": "AWAITING_MOCK_GROUP"
        }
        await query.edit_message_text(
            f"🏆 *Model Paper Selected:* `{q_count} Questions`\n\n"
            f"📢 *Send target Group Username or ID:*\n"
            f"Examples: `@myquizgroup` or `-1001234567890`\n\n"
            f"*(Ensure bot is an Admin in the group)*",
            parse_mode="Markdown"
        )


async def handle_subject_button_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    if data.startswith("sub_sel:"):
        key = data.split("sub_sel:")[1]
        subject_name = UP_SUPERTET_SUBJECTS.get(key, key)
        ADMIN_STATE[user_id] = {
            "custom_topic": subject_name,
            "step": "AWAITING_QUESTION_COUNT"
        }
        await query.edit_message_text(
            f"🎯 *Selected Subject:* `{subject_name}`\n\n"
            f"🔢 *Step 2/3: How many questions do you want to generate?*\n"
            f"Examples: `10`, `25`, `30`, `50`, `75`\n\n"
            f"👉 *Type and send the number of questions in chat:*",
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
    current_step = state.get("step")
    is_yt_link = ("youtube.com" in text or "youtu.be" in text)

    # Step: Admin providing YouTube URL
    if current_step == "AWAITING_YT_URL" or (is_yt_link and (not current_step or current_step == "AWAITING_PDF")):
        if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Unauthorized.")
            return

        status = await update.message.reply_text("⏳ *Step 1/3:* Fetching YouTube video & transcript...", parse_mode="Markdown")
        loop = asyncio.get_running_loop()
        title, transcript, err = await loop.run_in_executor(None, get_youtube_video_content, text)
        if err:
            await status.edit_text(f"❌ *YouTube Error:* `{err}`")
            return

        has_sub = "✅ Captions / Transcript Extracted" if transcript else "ℹ️ Topic & Concept Synthesis Mode"
        state["yt_url"] = text
        state["yt_title"] = title
        state["yt_transcript"] = transcript
        state["step"] = "AWAITING_YT_Q_COUNT"

        await status.edit_text(
            f"🎥 *YouTube Video Loaded:*\n`{title}`\n\n"
            f"📝 *Transcript:* `{has_sub}`\n\n"
            f"🔢 *Step 2/3: How many questions do you want to generate?*\n"
            f"Examples: `10`, `25`, `30`, `50`, `75`\n\n"
            f"👉 *Type and send the number of questions:*",
            parse_mode="Markdown"
        )
        return

    # Step: Admin providing YouTube question count
    if current_step == "AWAITING_YT_Q_COUNT":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Please enter a valid positive number (e.g. `25` or `50`):")
            return

        state["yt_q_count"] = int(text)
        state["step"] = "AWAITING_YT_GROUP"
        await update.message.reply_text(
            f"📊 *Questions to Generate from Video:* `{state['yt_q_count']}`\n\n"
            f"📢 *Step 3/3: Send target Group Username or ID:*\n"
            f"Examples: `@myquizgroup` or `-1001234567890`\n\n"
            f"*(Ensure bot is an Admin in the group)*",
            parse_mode="Markdown"
        )
        return

    # Step: Admin providing group for YouTube quiz
    if current_step == "AWAITING_YT_GROUP":
        target_group = text
        video_title = state.get("yt_title", "YouTube Class")
        transcript = state.get("yt_transcript", "")
        video_url = state.get("yt_url", "")
        q_count = state.get("yt_q_count", 25)

        status_msg = await update.message.reply_text(
            f"🧠 Synthesizing `{q_count}` questions from *{video_title}* with Gemini AI...",
            parse_mode="Markdown"
        )

        loop = asyncio.get_running_loop()
        mcqs, err = await loop.run_in_executor(None, generate_youtube_mcqs, video_title, transcript, video_url, q_count)

        if not mcqs:
            await status_msg.edit_text(f"❌ *Synthesis Failed:* `{err}`\nTry running again with `/start`.")
            return

        try:
            intro_card = (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *Exam Target:* `UP Super TET / Competitive`\n"
                f"🎥 *YouTube Source:* `{video_title[:45]}`\n"
                f"📊 *Questions:* `{len(mcqs)}`\n"
                f"⏱️ *Timer:* `25s per question`\n"
                f"✅ *Correct mark:* `+1.0`\n"
                f"➖ *Negative:* `None`\n"
                f"👥 *Voting:* `Open for ALL group members`\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🚀 *Starting now — tap your answer on each poll!*"
            )
            await context.bot.send_message(chat_id=target_group, text=intro_card, parse_mode="Markdown")
            group_chat_id = (await context.bot.get_chat(target_group)).id
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed to reach `{target_group}`: `{e}`\nCheck bot admin permissions and try again.")
            return

        state["step"] = "LAUNCHED"
        await status_msg.edit_text(f"🔥 *Quiz on `{video_title[:35]}` ({len(mcqs)} Qs) live in* `{target_group}`!", parse_mode="Markdown")

        shuffled_mcqs = shuffle_mcq_options(mcqs)
        ACTIVE_GROUP_QUIZZES[str(group_chat_id)] = {
            "mcqs": shuffled_mcqs,
            "current_index": 0,
            "topic": video_title[:40]
        }

        asyncio.create_task(run_telegram_quiz_engine(group_chat_id, context))
        return

    # Step: Admin providing custom model paper question count
    if current_step == "AWAITING_MOCK_COUNT":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Please enter a valid positive number of questions (e.g. `25`, `50`, `75`):")
            return

        count = int(text)
        state["mock_q_count"] = count
        state["step"] = "AWAITING_MOCK_GROUP"
        await update.message.reply_text(
            f"📊 *Full Model Paper Questions:* `{count}`\n\n"
            f"📢 *Send target Group Username or ID:*\n"
            f"Examples: `@myquizgroup` or `-1001234567890`\n\n"
            f"*(Ensure bot is an Admin in the group)*",
            parse_mode="Markdown"
        )
        return

    # Step: Admin providing group for full model paper quiz
    if current_step == "AWAITING_MOCK_GROUP":
        target_group = text
        q_count = state.get("mock_q_count", 25)

        status_msg = await update.message.reply_text(
            f"🧠 Synthesizing `{q_count}` Full Model Paper questions across all 14 UP Super TET subjects with Gemini AI...",
            parse_mode="Markdown"
        )

        loop = asyncio.get_running_loop()
        mcqs, err = await loop.run_in_executor(None, generate_full_model_paper_mcqs, q_count)

        if not mcqs:
            await status_msg.edit_text(f"❌ *Synthesis Failed:* `{err}`\nTry running again with `/start`.")
            return

        try:
            intro_card = (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *Exam Target:* `UP Super TET Full Model Paper (संपूर्ण मॉडल पेपर)`\n"
                f"📚 *Coverage:* `All 14 Subjects (Hindi, Sanskrit, Eng, Sci, Math, EV, CDP, Teaching Skills, GK/CA, Reasoning, IT, Life Skills)`\n"
                f"📊 *Questions:* `{len(mcqs)}`\n"
                f"⏱️ *Timer:* `25s per question`\n"
                f"✅ *Correct mark:* `+1.0`\n"
                f"➖ *Negative:* `None`\n"
                f"👥 *Voting:* `Open for ALL group members`\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🚀 *Starting now — tap your answer on each poll!*"
            )
            await context.bot.send_message(chat_id=target_group, text=intro_card, parse_mode="Markdown")
            group_chat_id = (await context.bot.get_chat(target_group)).id
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed to reach `{target_group}`: `{e}`\nCheck bot admin permissions and try again.")
            return

        state["step"] = "LAUNCHED"
        await status_msg.edit_text(f"🔥 *UP Super TET Full Model Paper ({len(mcqs)} Questions) live in* `{target_group}`!", parse_mode="Markdown")

        shuffled_mcqs = shuffle_mcq_options(mcqs)
        ACTIVE_GROUP_QUIZZES[str(group_chat_id)] = {
            "mcqs": shuffled_mcqs,
            "current_index": 0,
            "topic": "UP Super TET Full Model Paper"
        }

        asyncio.create_task(run_telegram_quiz_engine(group_chat_id, context))
        return

    # Step: Admin providing topic name
    if current_step == "AWAITING_TOPIC_NAME":
        state["custom_topic"] = text
        state["step"] = "AWAITING_QUESTION_COUNT"
        await update.message.reply_text(
            f"🎯 *Topic Selected:* `{text}`\n\n"
            f"🔢 *Step 2/3: How many questions do you want to generate?*\n"
            f"Examples: `10`, `25`, `30`, `50`, `75`\n\n"
            f"👉 *Send the number of questions:*",
            parse_mode="Markdown"
        )
        return

    # Step: Admin providing question count
    if current_step == "AWAITING_QUESTION_COUNT":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Please enter a valid positive number (e.g. `25` or `50`):")
            return

        count = int(text)
        state["q_count"] = count
        state["step"] = "AWAITING_TOPIC_GROUP"
        await update.message.reply_text(
            f"📊 *Questions to Generate:* `{count}`\n\n"
            f"📢 *Step 3/3: Send target Group Username or ID:*\n"
            f"Examples: `@myquizgroup` or `-1001234567890`\n\n"
            f"*(Ensure bot is an Admin in the group)*",
            parse_mode="Markdown"
        )
        return

    # Step: Admin providing group for topic quiz
    if current_step == "AWAITING_TOPIC_GROUP":
        target_group = text
        topic = state.get("custom_topic", "General Section")
        q_count = state.get("q_count", 25)

        status_msg = await update.message.reply_text(
            f"🧠 Generating `{q_count}` questions on *{topic}* based on latest UP Super TET exam pattern with Gemini AI...",
            parse_mode="Markdown"
        )

        loop = asyncio.get_running_loop()
        mcqs, err = await loop.run_in_executor(None, generate_custom_topic_mcqs, topic, q_count)

        if not mcqs:
            await status_msg.edit_text(f"❌ *Synthesis Failed:* `{err}`\nTry running again with `/start`.")
            return

        try:
            intro_card = (
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *Exam Target:* `UP Super TET / Competitive`\n"
                f"📖 *Topic:* `{topic}`\n"
                f"📊 *Questions:* `{len(mcqs)}`\n"
                f"⏱️ *Timer:* `25s per question`\n"
                f"✅ *Correct mark:* `+1.0`\n"
                f"➖ *Negative:* `None`\n"
                f"👥 *Voting:* `Open for ALL group members`\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🚀 *Starting now — tap your answer on each poll!*"
            )
            await context.bot.send_message(chat_id=target_group, text=intro_card, parse_mode="Markdown")
            group_chat_id = (await context.bot.get_chat(target_group)).id
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed to reach `{target_group}`: `{e}`\nCheck bot admin permissions and try again.")
            return

        state["step"] = "LAUNCHED"
        await status_msg.edit_text(f"🔥 *Quiz on `{topic}` ({len(mcqs)} Questions) live in* `{target_group}`!", parse_mode="Markdown")

        shuffled_mcqs = shuffle_mcq_options(mcqs)
        ACTIVE_GROUP_QUIZZES[str(group_chat_id)] = {
            "mcqs": shuffled_mcqs,
            "current_index": 0,
            "topic": topic
        }

        asyncio.create_task(run_telegram_quiz_engine(group_chat_id, context))
        return

    # PDF Question Count Step
    if current_step == "AWAITING_PDF_Q_COUNT":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Please enter a valid number of questions (e.g. `25` or `50`):")
            return

        state["pdf_q_count"] = int(text)
        state["step"] = "AWAITING_GROUP"
        await update.message.reply_text(
            f"📊 *Questions to Test:* `{state['pdf_q_count']}`\n\n"
            f"📢 *Now send target Group Username or ID:*\n"
            f"Examples: `@myquizgroup` or `-1001234567890`\n\n"
            f"*(Ensure bot is an Admin in the group)*",
            parse_mode="Markdown"
        )
        return

    # PDF Group Dispatcher Step
    if current_step == "AWAITING_GROUP":
        await handle_admin_group_input(update, context)
        return

    # Direct Web URL / Google Drive link
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
    state["step"] = "AWAITING_PDF_Q_COUNT"

    await query.edit_message_text(
        f"🏷️ *Selected Section:* `{chosen_topic}`\n\n"
        f"🔢 *How many questions do you want to test from this section?*\n"
        f"Examples: `10`, `25`, `30`, `50`, `75`\n\n"
        f"👉 *Send the number of questions:*",
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
    
    requested_count = state.get("pdf_q_count", len(mcqs))
    if requested_count < len(mcqs):
        mcqs = mcqs[:requested_count]

    try:
        intro_card = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 *Section / Chapter:* `{topic}`\n"
            f"📊 *Questions:* `{len(mcqs)}`\n"
            f"⏱️ *Timer:* `25s per question`\n"
            f"✅ *Correct mark:* `+1.0`\n"
            f"➖ *Negative:* `None`\n"
            f"👥 *Voting:* `Open for ALL group members`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🚀 *Starting now — tap your answer on each poll!*"
        )
        await context.bot.send_message(chat_id=target_group, text=intro_card, parse_mode="Markdown")
        group_chat_id = (await context.bot.get_chat(target_group)).id
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to reach `{target_group}`: `{e}`\nCheck bot admin permissions and try again:")
        return

    state["step"] = "LAUNCHED"
    await update.message.reply_text(f"🔥 *Quiz on `{topic}` ({len(mcqs)} Questions) initiated in* `{target_group}`!", parse_mode="Markdown")

    shuffled_mcqs = shuffle_mcq_options(mcqs)
    ACTIVE_GROUP_QUIZZES[str(group_chat_id)] = {
        "mcqs": shuffled_mcqs,
        "current_index": 0,
        "topic": topic
    }

    asyncio.create_task(run_telegram_quiz_engine(group_chat_id, context))


import random


def shuffle_mcq_options(mcqs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Eliminates LLM positional bias (Option A) by uniformly randomizing option order."""
    shuffled = []
    for item in mcqs:
        item_copy = dict(item)
        raw_options = [str(opt).strip() for opt in item_copy.get("options", []) if str(opt).strip()]
        if len(raw_options) < 2:
            shuffled.append(item_copy)
            continue

        orig_idx = int(item_copy.get("correct_index", 0))
        if orig_idx < 0 or orig_idx >= len(raw_options):
            orig_idx = 0

        correct_val = raw_options[orig_idx]
        opts = list(raw_options)
        random.shuffle(opts)
        new_idx = opts.index(correct_val)

        item_copy["options"] = opts
        item_copy["correct_index"] = new_idx
        shuffled.append(item_copy)
    return shuffled


# Global Poll Lookup for Live Vote Tracking
POLL_LOOKUP: Dict[str, Dict[str, Any]] = {}


# ----------------- TELEGRAM NATIVE QUIZ ENGINE (ALL-USER VOTING & RANKING) -----------------
async def run_telegram_quiz_engine(group_chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    chat_key = str(group_chat_id)
    session = ACTIVE_GROUP_QUIZZES.get(chat_key)
    if not session:
        return

    mcqs = session["mcqs"]
    total_q = len(mcqs)
    session["scores"] = {}

    for idx, item in enumerate(mcqs):
        session["current_index"] = idx

        raw_q = item.get("question", "").strip()
        q_title = f"[{idx + 1}/{total_q}] {raw_q}"
        if len(q_title) > 295:
            q_title = q_title[:292] + "..."

        # Deduplicate and format options (strictly max 95 characters per Telegram limit)
        raw_options = item.get("options", [])
        options = []
        seen = set()
        for opt in raw_options:
            s_opt = str(opt).strip()[:95]
            if not s_opt:
                continue
            if s_opt in seen:
                s_opt = f"{s_opt}."
            seen.add(s_opt)
            options.append(s_opt)

        if len(options) < 2:
            options = ["(A) विकल्प 1", "(B) विकल्प 2", "(C) विकल्प 3", "(D) विकल्प 4"]

        correct_id = int(item.get("correct_index", 0))
        if correct_id < 0 or correct_id >= len(options):
            correct_id = 0

        # Explanation shown natively when user taps wrong or taps lightbulb 💡
        explanation = item.get("pro_tip", "") or item.get("concept", "") or item.get("solution", "")
        explanation = explanation.strip().replace("`", "").replace("*", "")
        if len(explanation) > 195:
            explanation = explanation[:192] + "..."

        poll_msg = None
        # Attempt non-anonymous quiz poll with 25s timer
        try:
            poll_msg = await context.bot.send_poll(
                chat_id=group_chat_id,
                question=q_title,
                options=options,
                type="quiz",
                correct_option_id=correct_id,
                explanation=explanation if explanation else None,
                is_anonymous=False,
                open_period=25
            )
        except Exception as p_err1:
            logger.warning(f"Poll attempt 1 failed: {p_err1}")
            try:
                poll_msg = await context.bot.send_poll(
                    chat_id=group_chat_id,
                    question=q_title,
                    options=options,
                    type="quiz",
                    correct_option_id=correct_id,
                    is_anonymous=False,
                    open_period=25
                )
            except Exception as p_err2:
                logger.warning(f"Poll attempt 2 failed: {p_err2}")
                try:
                    poll_msg = await context.bot.send_poll(
                        chat_id=group_chat_id,
                        question=q_title,
                        options=options,
                        type="quiz",
                        correct_option_id=correct_id,
                        is_anonymous=False
                    )
                except Exception as p_err3:
                    logger.warning(f"Poll attempt 3 failed: {p_err3}")
                    try:
                        poll_msg = await context.bot.send_poll(
                            chat_id=group_chat_id,
                            question=q_title,
                            options=options,
                            type="quiz",
                            correct_option_id=correct_id,
                            is_anonymous=True
                        )
                    except Exception as fb_err:
                        logger.error(f"All poll dispatch fallbacks failed: {fb_err}")

        if poll_msg and poll_msg.poll:
            POLL_LOOKUP[poll_msg.poll.id] = {
                "chat_key": chat_key,
                "correct_id": correct_id
            }

        # 1. Wait for 25-second voting period to allow all group members to choose their option
        await asyncio.sleep(25)

        # 2. Post the Clean Solution Card
        full_solution = item.get("solution", "") or item.get("concept", "No additional notes.")
        pro_tip = item.get("pro_tip", "")
        letters = ["A", "B", "C", "D"]
        correct_letter = letters[correct_id] if correct_id < len(letters) else str(correct_id + 1)
        correct_text = options[correct_id] if correct_id < len(options) else "Correct Option"

        solution_card = (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *ANSWER & SOLUTION (Q {idx + 1}/{total_q})*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ *Correct Answer:* Option {correct_letter} — {correct_text}\n\n"
            f"📝 *Detailed Explanation:*\n{full_solution}\n\n"
        )
        if pro_tip:
            solution_card += f"💡 *Exam Trick & Key Rule:*\n`{pro_tip}`\n\n"

        source_ref = item.get("source_ref", "")
        if source_ref:
            solution_card += f"📚 *Source Ref:* `{source_ref}`\n\n"

        solution_card += f"⏳ _Next question starting in 4 seconds..._"

        try:
            reply_id = poll_msg.message_id if poll_msg else None
            await context.bot.send_message(
                chat_id=group_chat_id,
                text=solution_card,
                reply_to_message_id=reply_id,
                parse_mode="Markdown"
            )
        except Exception as sol_err:
            logger.error(f"Solution post error: {sol_err}")

        # 3. Intermission before next question
        await asyncio.sleep(4)

    # ----------------- FINAL TEST COMPLETION & RANKING LEADERBOARD -----------------
    scores = session.get("scores", {})
    leaderboard_lines = [
        "━━━━━━━━━━━━━━━━━━━━━",
        "🏆 *TEST LEADERBOARD & FINAL RANKING*",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📖 *Topic:* `{session['topic']}`",
        f"📊 *Total Questions:* `{total_q}`\n"
    ]

    if scores:
        sorted_users = sorted(
            scores.values(),
            key=lambda x: (x["correct"], -x["attempts"]),
            reverse=True
        )
        medals = ["🥇", "🥈", "🥉", "🎖️", "🎖️", "🎖️", "🎖️", "🎖️", "🎖️", "🎖️"]
        for rank, u in enumerate(sorted_users[:15]):
            medal = medals[rank] if rank < len(medals) else "👤"
            correct = u["correct"]
            att = u["attempts"]
            pct = int((correct / total_q) * 100)
            leaderboard_lines.append(
                f"{medal} *Rank {rank + 1}:* {u['name']} — `{correct}/{total_q}` Correct ({pct}% Accuracy) • {att} Att"
            )
        leaderboard_lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━\n👥 *Total Candidates Attempted:* `{len(scores)}`")
        leaderboard_lines.append("🎉 *Congratulations to all top performers!*")
    else:
        leaderboard_lines.append("🎉 *Quiz session completed! Good effort everyone.*")

    leaderboard_lines.append("\n👉 *To launch the next test on any topic or PDF, send `/start` in bot DM.*")

    await context.bot.send_message(
        chat_id=group_chat_id,
        text="\n".join(leaderboard_lines),
        parse_mode="Markdown"
    )
    ACTIVE_GROUP_QUIZZES.pop(chat_key, None)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks live participant votes and computes real-time candidate scores."""
    answer = update.poll_answer
    poll_id = answer.poll_id
    user = answer.user
    selected = answer.option_ids[0] if answer.option_ids else -1

    if poll_id in POLL_LOOKUP:
        info = POLL_LOOKUP[poll_id]
        chat_key = info["chat_key"]
        session = ACTIVE_GROUP_QUIZZES.get(chat_key)
        if session:
            user_id = user.id
            if user_id not in session["scores"]:
                name = user.first_name or "Candidate"
                if user.last_name:
                    name += f" {user.last_name}"
                session["scores"][user_id] = {
                    "name": name,
                    "correct": 0,
                    "attempts": 0
                }
            session["scores"][user_id]["attempts"] += 1
            if selected == info["correct_id"]:
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
    app.add_handler(CallbackQueryHandler(handle_mode_callback, pattern=r"^mode_"))
    app.add_handler(CallbackQueryHandler(handle_mock_selection, pattern=r"^mock_len:"))
    app.add_handler(CallbackQueryHandler(handle_subject_button_selection, pattern=r"^sub_sel:"))
    app.add_handler(CallbackQueryHandler(handle_admin_topic_choice, pattern=r"^adm_top:"))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    logger.info("Native QuizBot engine active with full group voting and leaderboard rankings.")
    app.run_polling(drop_pending_updates=True, poll_interval=1.0, timeout=30)


if __name__ == "__main__":
    main()
