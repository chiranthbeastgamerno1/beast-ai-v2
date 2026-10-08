import os
import json
import urllib.parse
import urllib.request
import urllib.error
import random
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS

# 🚀 GUARDED IMPORT: Prevents Render from crashing on startup if google-genai is missing
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

app = Flask(__name__)
# 🚀 ALLOWS FRONTEND TO TALK TO RENDER BACKEND
CORS(app, resources={r"/api/*": {"origins": "*"}}) 

# 🚀 BROWSER USER-AGENT: Prevents 403 blocks from Cloudflare-protected APIs
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Load Gemini Keys
api_keys = [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
    os.environ.get("GEMINI_API_KEY_5"),
    os.environ.get("GEMINI_API_KEY_6"),
    os.environ.get("GEMINI_API_KEY_7"),
    os.environ.get("GEMINI_API_KEY_8"),
    os.environ.get("GEMINI_API_KEY_9")
]
valid_keys = [key for key in api_keys if key and key.strip()]

@app.route('/')
def home():
    return "Beast AI Core is Online (Render Server)! 🦖✨"

@app.route('/api/health', methods=['GET'])
def health_check():
    # Helpful debug route to verify backend status
    return jsonify({
        "status": "Online",
        "genai_installed": GENAI_AVAILABLE,
        "valid_gemini_keys": len(valid_keys)
    }), 200

@app.route('/api/chat', methods=['POST'])
def chat():
    start_time = time.time()
    try:
        message = request.form.get("message", "")
        mode = request.form.get("mode", "chat")
        speed = request.form.get("speed", "normal")
        files = request.files.getlist("files") if hasattr(request, 'files') else []
        history_json = request.form.get("history", "[]")
        
        try: chat_history = json.loads(history_json)
        except: chat_history = []

        if not message and not files:
            return jsonify({"reply": "The Beast hears only silence. 🤫"}), 200

        # 🚀 FIXED: Read files ONCE and cache bytes so they don't break during key rotation
        uploaded_files = []
        if files:
            for f in files:
                uploaded_files.append({"bytes": f.read(), "mime_type": f.content_type})

        ist = timezone(timedelta(hours=5, minutes=30))
        live_time = datetime.now(ist).strftime("%A, %d %B %Y, %I:%M %p IST")

        # ==========================================
        # 1. KIRA 3.0 IMAGE GENERATION 
        # ==========================================
        if mode == 'image':
            img_key = os.environ.get("KIRA_IMAGE_API_KEY")
            img_url = None
            error_msg = ""
            
            if img_key:
                try:
                    url = "https://kiraai.vn/api/v1/images/generations"
                    headers = {"Authorization": f"Bearer {img_key}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
                    payload = json.dumps({"model": "kira-3.0-image", "prompt": message, "n": 1}).encode('utf-8')
                    req = urllib.request.Request(url, data=payload, headers=headers)
                    with urllib.request.urlopen(req, timeout=15) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        img_url = data['data'][0]['url']
                except urllib.error.HTTPError as e:
                    error_msg = f"Kira HTTP Error {e.code}: {e.read().decode('utf-8')}"
                except Exception as e:
                    error_msg = str(e)
            else:
                error_msg = "KIRA_IMAGE_API_KEY is missing."

            if img_url:
                return jsonify({"reply": f"![Manifested Image]({img_url})"}), 200
            else:
                # 🚀 FALLBACK: Free Pollinations Image API
                seed = random.randint(1, 999999)
                safe_prompt = urllib.parse.quote(message)
                fallback_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?nologo=true&seed={seed}"
                return jsonify({"reply": f"![Manifested Image]({fallback_url})\n\n*(Note: Primary engine failed. Used fallback. Error: {error_msg})*"}), 200

        # ==========================================
        # 2. KIRA 3.0 VIDEO GENERATION
        # ==========================================
        elif mode in ['video', 'video-fast']:
            is_fast = (mode == 'video-fast')
            model_name = "kira-3.0-video-flash" if is_fast else "kira-3.0-video"
            vid_key = os.environ.get("KIRA_VIDEO_FLASH_API_KEY") if is_fast else os.environ.get("KIRA_VIDEO_API_KEY")
            
            if not vid_key:
                return jsonify({"reply": f"**Error:** Missing `{model_name}` Environment Variable in Render. 🦖"}), 200
            
            try:
                url = "https://kiraai.vn/api/v1/videos/generations"
                headers = {"Authorization": f"Bearer {vid_key}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
                payload = json.dumps({"model": model_name, "prompt": message}).encode('utf-8')
                req = urllib.request.Request(url, data=payload, headers=headers)
                
                # Render allows higher timeouts, expanding to 60s for video generation
                with urllib.request.urlopen(req, timeout=60) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    video_url = data.get('url') or data.get('data', [{}])[0].get('url', '')
                    return jsonify({"reply": video_url}), 200
            except urllib.error.HTTPError as e:
                return jsonify({"reply": f"**Kira Video API Error {e.code}:** `{e.read().decode('utf-8')}`"}), 200
            except Exception as e:
                return jsonify({"reply": f"**System Intercept Error:** `{str(e)}`"}), 200

        # ==========================================
        # 3. TEXT & CODING GENERATION
        # ==========================================
        else:
            final_response_text = None
            error_log = []
            system_instruction = (
                "You are Beast AI, a friendly and witty assistant. 🦖✨\n"
                f"- Current live time: {live_time}.\n"
                "- NEVER use italics (*text* or _text_). Always keep text completely normal unless using **bold**.\n"
                "RULES: If the user says 'hi', say hello normally. Keep answers direct. Use emojis! 🚀🔥"
            )

            # 🚀 FIXED: Skip Grok/Kira if files are attached (they can't read images in this setup)
            if not uploaded_files:
                # --- GROK 4.6 (PRO) ---
                if speed == 'pro':
                    grok_key = os.environ.get("GROK_API_KEY")
                    if grok_key:
                        try:
                            messages_payload = [{"role": "system", "content": system_instruction}]
                            for item in chat_history[-10:]:
                                role = "user" if item.get("type") == "user" else "assistant"
                                if item.get("message"): messages_payload.append({"role": role, "content": item.get("message")})
                            if message: messages_payload.append({"role": "user", "content": message})

                            url = "https://api.x.ai/v1/chat/completions"
                            headers = {"Authorization": f"Bearer {grok_key}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
                            payload = json.dumps({"model": "grok-beta", "messages": messages_payload}).encode('utf-8')
                            req = urllib.request.Request(url, data=payload, headers=headers)
                            with urllib.request.urlopen(req, timeout=12) as response:
                                data = json.loads(response.read().decode('utf-8'))
                                final_response_text = data['choices'][0]['message']['content']
                        except urllib.error.HTTPError as e:
                            error_log.append(f"Grok HTTP {e.code}: {e.read().decode('utf-8')}")
                        except Exception as e:
                            error_log.append(f"Grok Error: {str(e)}")

                # --- KIRA 3.5 FLASH (FAST) ---
                elif speed == 'fast':
                    flash_key = os.environ.get("KIRA_FLASH_API_KEY")
                    if flash_key:
                        try:
                            messages_payload = [{"role": "system", "content": system_instruction}]
                            for item in chat_history[-10:]:
                                role = "user" if item.get("type") == "user" else "assistant"
                                if item.get("message"): messages_payload.append({"role": role, "content": item.get("message")})
                            if message: messages_payload.append({"role": "user", "content": message})

                            url = "https://kiraai.vn/api/v1/chat/completions"
                            headers = {"Authorization": f"Bearer {flash_key}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
                            payload = json.dumps({"model": "kira-3.5-flash", "messages": messages_payload}).encode('utf-8')
                            req = urllib.request.Request(url, data=payload, headers=headers)
                            with urllib.request.urlopen(req, timeout=12) as response:
                                data = json.loads(response.read().decode('utf-8'))
                                final_response_text = data['choices'][0]['message']['content']
                        except urllib.error.HTTPError as e:
                            error_log.append(f"Kira Flash HTTP {e.code}: {e.read().decode('utf-8')}")
                        except Exception as e:
                            error_log.append(f"Kira Flash Error: {str(e)}")

            # --- GEMINI 2.5 FLASH (NORMAL) ---
            if not final_response_text and valid_keys:
                if not GENAI_AVAILABLE:
                    error_log.append("The 'google-genai' package is missing from requirements.txt.")
                else:
                    keys_to_try = list(valid_keys)
                    random.shuffle(keys_to_try)
                    for key in keys_to_try:
                        # Render has high timeouts, but cap Gemini at 20s to ensure fallback has time
                        if final_response_text or (time.time() - start_time > 20): break 
                        try:
                            client = genai.Client(api_key=key)
                            google_contents = []
                            for item in chat_history[-10:]:
                                role = "user" if item.get("type") == "user" else "model"
                                if item.get("message"): google_contents.append(types.Content(role=role, parts=[types.Part.from_text(text=item.get("message"))]))
                            
                            current_parts = []
                            if message: current_parts.append(types.Part.from_text(text=message))
                            
                            # Uses cached bytes to prevent seek() errors
                            if uploaded_files:
                                for f in uploaded_files: current_parts.append(types.Part.from_bytes(data=f["bytes"], mime_type=f["mime_type"]))
                            
                            if current_parts: google_contents.append(types.Content(role="user", parts=current_parts))

                            response = client.models.generate_content(
                                model='gemini-2.5-flash', 
                                contents=google_contents,
                                config=types.GenerateContentConfig(system_instruction=system_instruction)
                            )
                            if response.text:
                                final_response_text = response.text
                                break
                        except Exception as e:
                            if "safety" in str(e).lower(): return jsonify({"reply": "The Beast safety shields blocked this request! 🛡✨"}), 200
                            error_log.append(f"Gemini Error: {str(e)}")
                            continue 
            elif not final_response_text and not valid_keys:
                error_log.append("No active Gemini API keys found.")

            # 🚀 THE RANDOM FALLBACK: Free Pollinations Text API
            # Guarantees the server will never send back a blank "recalibrating" error again.
            if not final_response_text:
                try:
                    safe_prompt = urllib.parse.quote(message)
                    url = f"https://text.pollinations.ai/{safe_prompt}?system={urllib.parse.quote(system_instruction)}"
                    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
                    with urllib.request.urlopen(req, timeout=10) as response:
                        fallback_text = response.read().decode('utf-8')
                        errors_str = " | ".join(error_log)
                        final_response_text = f"{fallback_text}\n\n*(Note: Primary engines failed. Fallback used. Reason: {errors_str})*"
                except Exception as e:
                    errors_str = " | ".join(error_log)
                    final_response_text = f"**System Failure.** All APIs failed.\n\n**Details:**\n{errors_str}\nFallback Error: {str(e)}"

            return jsonify({"reply": final_response_text}), 200

    except Exception as e:
        return jsonify({"reply": f"**System Intercept Error:** `{str(e)}`"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
