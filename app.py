import os
import json
import random
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS

# Guarded import: if google-genai is missing from requirements.txt the whole app
# used to crash on startup (that is the "connection severed" error). Now it just
# skips Gemini and tells you why.
try:
    from google import genai
    from google.genai import types
    GENAI_IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover
    genai = None
    types = None
    GENAI_IMPORT_ERROR = str(_e)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

PLATFORM = "Render Server"
# Total time budget (seconds) for one request. Render has no 10s limit; gunicorn --timeout 120 covers this.
TIME_BUDGET = 110

# Model names live in environment variables so a retired model never needs a code change.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4")
KIRA_TEXT_MODEL = os.environ.get("KIRA_TEXT_MODEL", "kira-3.5-flash")
KIRA_IMAGE_MODEL = os.environ.get("KIRA_IMAGE_MODEL", "kira-3.0-image")
KIRA_VIDEO_MODEL = os.environ.get("KIRA_VIDEO_MODEL", "kira-3.0-video")
KIRA_VIDEO_FLASH_MODEL = os.environ.get("KIRA_VIDEO_FLASH_MODEL", "kira-3.0-video-flash")

KIRA_BASES = ["https://kiraai.vn/api/v1", "https://api.kira.ai/v1"]
GROK_URL = "https://api.x.ai/v1/chat/completions"


def load_gemini_keys():
    names = ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(1, 10)]
    keys = []
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v and v not in keys:
            keys.append(v)
    return keys


def time_left(start):
    return TIME_BUDGET - (time.time() - start)


def short(text, n=220):
    text = str(text).replace("\n", " ")
    return text if len(text) <= n else text[:n] + "..."


def http_error_text(e):
    try:
        body = e.read().decode("utf-8", "ignore")
    except Exception:
        body = ""
    return f"HTTP {e.code} {short(body)}"


def post_json(url, api_key, payload, timeout):
    # A browser-like User-Agent matters: Cloudflare-protected APIs (xAI, many
    # resellers) reject the default "Python-urllib" agent with a 403.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; BeastAI/1.0)",
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=max(3, timeout)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_openai_messages(system_instruction, chat_history, message):
    msgs = [{"role": "system", "content": system_instruction}]
    for item in chat_history[-10:]:
        role = "user" if item.get("type") == "user" else "assistant"
        if item.get("message"):
            msgs.append({"role": role, "content": item["message"]})
    if message:
        msgs.append({"role": "user", "content": message})
    return msgs


def extract_media_url(data):
    if not isinstance(data, dict):
        return None
    for k in ("url", "video_url", "image_url"):
        if data.get(k):
            return data[k]
    inner = data.get("data")
    if isinstance(inner, list) and inner and isinstance(inner[0], dict):
        for k in ("url", "video_url", "image_url"):
            if inner[0].get(k):
                return inner[0][k]
    return None


def try_grok(system_instruction, chat_history, message, start, errors):
    key = os.environ.get("GROK_API_KEY")
    if not key:
        errors.append("GROK_API_KEY is not set")
        return None
    try:
        data = post_json(GROK_URL, key, {
            "model": GROK_MODEL,
            "messages": build_openai_messages(system_instruction, chat_history, message),
        }, min(60, time_left(start)))
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        errors.append(f"Grok ({GROK_MODEL}) {http_error_text(e)}")
    except Exception as e:
        errors.append(f"Grok error: {short(e)}")
    return None


def try_kira_text(system_instruction, chat_history, message, start, errors):
    key = os.environ.get("KIRA_FLASH_API_KEY")
    if not key:
        errors.append("KIRA_FLASH_API_KEY is not set")
        return None
    payload = {"model": KIRA_TEXT_MODEL, "messages": build_openai_messages(system_instruction, chat_history, message)}
    for base in KIRA_BASES:
        try:
            data = post_json(f"{base}/chat/completions", key, payload, min(40, time_left(start)))
            text = data["choices"][0]["message"]["content"]
            if text:
                return text
        except urllib.error.HTTPError as e:
            errors.append(f"Kira Flash {base} {http_error_text(e)}")
        except Exception as e:
            errors.append(f"Kira Flash {base} error: {short(e)}")
    return None


def try_gemini(system_instruction, chat_history, message, file_blobs, start, errors):
    if genai is None:
        errors.append(f"google-genai not installed ({short(GENAI_IMPORT_ERROR)}). Add google-genai to requirements.txt")
        return None
    keys = load_gemini_keys()
    if not keys:
        errors.append("No GEMINI_API_KEY / GEMINI_API_KEY_1..9 set")
        return None
    random.shuffle(keys)
    for key in keys:
        if time_left(start) < 4:
            errors.append("Out of time before all Gemini keys were tried")
            break
        try:
            client = genai.Client(api_key=key)
            contents = []
            for item in chat_history[-10:]:
                role = "user" if item.get("type") == "user" else "model"
                if item.get("message"):
                    contents.append(types.Content(role=role, parts=[types.Part.from_text(text=item["message"])]))
            parts = []
            if message:
                parts.append(types.Part.from_text(text=message))
            for data_bytes, mime in file_blobs:  # bytes were read ONCE up front, so retries still have them
                parts.append(types.Part.from_bytes(data=data_bytes, mime_type=mime))
            if parts:
                contents.append(types.Content(role="user", parts=parts))
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_instruction),
            )
            if response.text:
                return response.text
            errors.append("Gemini returned empty text (possibly blocked by safety filters)")
        except Exception as e:
            errors.append(f"Gemini ({GEMINI_MODEL}) error: {short(e)}")
    return None


def try_pollinations_text(system_instruction, message, errors):
    try:
        url = "https://text.pollinations.ai/" + urllib.parse.quote(message or "hello") + "?system=" + urllib.parse.quote(system_instruction)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BeastAI/1.0)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        errors.append(f"Pollinations fallback error: {short(e)}")
        return None


@app.route("/")
def home():
    return f"Beast AI Core is Online ({PLATFORM})! 🦖✨"


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "platform": PLATFORM,
        "genai_installed": genai is not None,
        "gemini_keys_found": len(load_gemini_keys()),
        "env_present": {n: bool(os.environ.get(n)) for n in [
            "GROK_API_KEY", "KIRA_FLASH_API_KEY", "KIRA_IMAGE_API_KEY",
            "KIRA_VIDEO_API_KEY", "KIRA_VIDEO_FLASH_API_KEY"]},
    })


@app.route("/api/chat", methods=["POST"])
def chat():
    start = time.time()
    try:
        message = request.form.get("message", "")
        mode = request.form.get("mode", "chat")
        speed = request.form.get("speed", "normal")
        uploaded = request.files.getlist("files")
        try:
            chat_history = json.loads(request.form.get("history", "[]"))
            if not isinstance(chat_history, list):
                chat_history = []
        except Exception:
            chat_history = []

        # Read uploaded files exactly once.
        file_blobs = [(f.read(), f.mimetype or "application/octet-stream") for f in uploaded]

        if not message and not file_blobs:
            return jsonify({"reply": "The Beast hears only silence. 🤫"}), 200

        ist = timezone(timedelta(hours=5, minutes=30))
        live_time = datetime.now(ist).strftime("%A, %d %B %Y, %I:%M %p IST")

        # ---------------- IMAGE ----------------
        if mode == "image":
            key = os.environ.get("KIRA_IMAGE_API_KEY")
            errors = []
            img_url = None
            if key:
                for base in KIRA_BASES:
                    try:
                        data = post_json(f"{base}/images/generations", key,
                                         {"model": KIRA_IMAGE_MODEL, "prompt": message, "n": 1}, min(40, time_left(start)))
                        img_url = extract_media_url(data)
                        if img_url:
                            break
                        errors.append(f"Kira Image {base}: no image URL in response: {short(json.dumps(data))}")
                    except urllib.error.HTTPError as e:
                        errors.append(f"Kira Image {base} {http_error_text(e)}")
                    except Exception as e:
                        errors.append(f"Kira Image {base} error: {short(e)}")
            else:
                errors.append("KIRA_IMAGE_API_KEY is not set")
            if img_url:
                return jsonify({"reply": f"![Manifested Image]({img_url})"}), 200
            seed = random.randint(1, 999999)
            fallback = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(message)}?nologo=true&seed={seed}"
            return jsonify({"reply": f"![Manifested Image]({fallback})\n\nNote: Kira image failed, so a free fallback engine was used. Reason: {' | '.join(errors)}"}), 200

        # ---------------- VIDEO ----------------
        if mode in ("video", "video-fast"):
            is_fast = mode == "video-fast"
            model_name = KIRA_VIDEO_FLASH_MODEL if is_fast else KIRA_VIDEO_MODEL
            env_name = "KIRA_VIDEO_FLASH_API_KEY" if is_fast else "KIRA_VIDEO_API_KEY"
            key = os.environ.get(env_name)
            if not key:
                return jsonify({"reply": f"**Config error:** `{env_name}` is not set in {PLATFORM} environment variables."}), 200
            errors = []
            for base in KIRA_BASES:
                try:
                    data = post_json(f"{base}/videos/generations", key,
                                     {"model": model_name, "prompt": message}, min(100, time_left(start)))
                    url = extract_media_url(data)
                    if url:
                        return jsonify({"reply": url}), 200
                    errors.append(f"{base}: no video URL in response: {short(json.dumps(data))}")
                except urllib.error.HTTPError as e:
                    errors.append(f"{base} {http_error_text(e)}")
                except Exception as e:
                    errors.append(f"{base} error: {short(e)}")
            return jsonify({"reply": "**Kira video generation failed:** `" + " | ".join(errors) + "`"}), 200

        # ---------------- TEXT / CODE ----------------
        system_instruction = (
            "You are Beast AI, a friendly and witty assistant. 🦖✨\n"
            f"- Current live time: {live_time}.\n"
            "- NEVER use italics (*text* or _text_). Always keep text completely normal unless using **bold**.\n"
            "RULES: If the user says 'hi', say hello normally. Keep answers direct. Use emojis! 🚀🔥"
        )
        errors = []
        text = None

        # Images can only be understood by Gemini, so skip Grok/Kira when files are attached.
        if not file_blobs:
            if speed == "pro":
                text = try_grok(system_instruction, chat_history, message, start, errors)
            elif speed == "fast":
                text = try_kira_text(system_instruction, chat_history, message, start, errors)

        if not text:
            text = try_gemini(system_instruction, chat_history, message, file_blobs, start, errors)

        if not text:
            fb = try_pollinations_text(system_instruction, message, errors)
            if fb:
                text = f"{fb}\n\nNote: primary engines failed, free fallback used. Reasons: {' | '.join(errors)}"

        if not text:
            text = "**All engines failed.**\n\n" + "\n".join(f"- {e}" for e in errors)

        return jsonify({"reply": text}), 200

    except Exception as e:
        return jsonify({"reply": f"**System error:** `{short(e, 400)}`"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
