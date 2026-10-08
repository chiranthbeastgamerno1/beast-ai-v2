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
from google import genai
from google.genai import types

app = Flask(__name__)
# 🚀 ALLOWS FRONTEND TO TALK TO RENDER BACKEND
CORS(app, resources={r"/api/*": {"origins": "*"}}) 

# 🔑 Load Gemini API keys (keys 1 through 9 + default)
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

@app.route('/api/chat', methods=['POST'])
def chat():
    start_time = time.time()
    try:
        message = request.form.get("message", "")
        mode = request.form.get("mode", "chat")
        speed = request.form.get("speed", "normal")
        files = request.files.getlist("files") if hasattr(request, 'files') else []
        history_json = request.form.get("history", "[]")
        
        try:
            chat_history = json.loads(history_json)
        except Exception:
            chat_history = []

        if not message and not files:
            return jsonify({"reply": "The Beast hears only silence. 🤫"}), 200

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
                endpoints = [
                    "https://kiraai.vn/api/v1/images/generations",
                    "https://api.kira.ai/v1/images/generations"
                ]
                payload = json.dumps({"model": "kira-3.0-image", "prompt": message, "n": 1}).encode('utf-8')
                headers = {"Authorization": f"Bearer {img_key}", "Content-Type": "application/json"}
                
                for endpoint in endpoints:
                    try:
                        req = urllib.request.Request(endpoint, data=payload, headers=headers)
                        with urllib.request.urlopen(req, timeout=15) as response:
                            data = json.loads(response.read().decode('utf-8'))
                            img_url = data['data'][0]['url']
                            if img_url:
                                break
                    except urllib.error.HTTPError as e:
                        error_msg = f"Kira Image Error {e.code}: {e.read().decode('utf-8')}"
                    except Exception as e:
                        error_msg = f"Kira Image Error: {str(e)}"
            else:
                error_msg = "KIRA_IMAGE_API_KEY is not configured in Render."

            if img_url:
                return jsonify({"reply": f"![Manifested Image]({img_url})"}), 200
            else:
                # 🚀 FALLBACK: Free Pollinations Image generation engine
                seed = random.randint(1, 999999)
                safe_prompt = urllib.parse.quote(message)
                fallback_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?nologo=true&seed={seed}"
                return jsonify({"reply": f"![Manifested Image]({fallback_url})\n\n*(Note: Primary engine failed. Fallback utilized. Reason: {error_msg})*"}), 200

        # ==========================================
        # 2. KIRA 3.0 VIDEO GENERATION (Normal & Fast)
        # ==========================================
        elif mode in ['video', 'video-fast']:
            is_fast = (mode == 'video-fast')
            model_name = "kira-3.0-video-flash" if is_fast else "kira-3.0-video"
            vid_key = os.environ.get("KIRA_VIDEO_FLASH_API_KEY") if is_fast else os.environ.get("KIRA_VIDEO_API_KEY")
            
            if not vid_key:
                return jsonify({"reply": f"**Config Error:** `{model_name}` key is missing from environment variables."}), 200
            
            endpoints = [
                "https://kiraai.vn/api/v1/videos/generations",
                "https://api.kira.ai/v1/videos/generations"
            ]
            payload = json.dumps({"model": model_name, "prompt": message}).encode('utf-8')
            headers = {"Authorization": f"Bearer {vid_key}", "Content-Type": "application/json"}
            video_url = None
            err_msg = ""
            
            for endpoint in endpoints:
                try:
                    req = urllib.request.Request(endpoint, data=payload, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        video_url = data.get('url') or data.get('data', [{}])[0].get('url', '')
                        if video_url:
                            break
                except urllib.error.HTTPError as e:
                    err_msg = f"Kira Video Error {e.code}: {e.read().decode('utf-8')}"
                except Exception as e:
                    err_msg = f"Kira Video Error: {str(e)}"

            if video_url:
                return jsonify({"reply": video_url}), 200
            else:
                return jsonify({"reply": f"**Kira Video Generation Error:** `{err_msg}`"}), 200

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

            # --- GROK 4.6 (PRO: Coding & Reasoning) ---
            if speed == 'pro':
                grok_key = os.environ.get("GROK_API_KEY")
                if grok_key:
                    try:
                        messages_payload = [{"role": "system", "content": system_instruction}]
                        for item in chat_history[-10:]:
                            role = "user" if item.get("type") == "user" else "assistant"
                            if item.get("message"):
                                messages_payload.append({"role": role, "content": item.get("message")})
                        if message:
                            messages_payload.append({"role": "user", "content": message})

                        url = "https://api.x.ai/v1/chat/completions"
                        headers = {"Authorization": f"Bearer {grok_key}", "Content-Type": "application/json"}
                        payload = json.dumps({"model": "grok-beta", "messages": messages_payload}).encode('utf-8')
                        req = urllib.request.Request(url, data=payload, headers=headers)
                        with urllib.request.urlopen(req, timeout=9) as response:
                            data = json.loads(response.read().decode('utf-8'))
                            final_response_text = data['choices'][0]['message']['content']
                    except urllib.error.HTTPError as e:
                        error_log.append(f"Grok HTTP Error {e.code}: {e.read().decode('utf-8')}")
                    except Exception as e:
                        error_log.append(f"Grok Error: {str(e)}")
                else:
                    error_log.append("GROK_API_KEY is not configured.")

            # --- KIRA 3.5 FLASH (FAST TEXT) ---
            elif speed == 'fast':
                flash_key = os.environ.get("KIRA_FLASH_API_KEY")
                if flash_key:
                    endpoints = [
                        "https://kiraai.vn/api/v1/chat/completions",
                        "https://api.kira.ai/v1/chat/completions"
                    ]
                    messages_payload = [{"role": "system", "content": system_instruction}]
                    for item in chat_history[-10:]:
                        role = "user" if item.get("type") == "user" else "assistant"
                        if item.get("message"):
                            messages_payload.append({"role": role, "content": item.get("message")})
                    if message:
                        messages_payload.append({"role": "user", "content": message})

                    payload = json.dumps({"model": "kira-3.5-flash", "messages": messages_payload}).encode('utf-8')
                    headers = {"Authorization": f"Bearer {flash_key}", "Content-Type": "application/json"}
                    
                    for endpoint in endpoints:
                        try:
                            req = urllib.request.Request(endpoint, data=payload, headers=headers)
                            with urllib.request.urlopen(req, timeout=8) as response:
                                data = json.loads(response.read().decode('utf-8'))
                                final_response_text = data['choices'][0]['message']['content']
                                if final_response_text:
                                    break
                        except urllib.error.HTTPError as e:
                            error_log.append(f"Kira Flash Error {e.code}: {e.read().decode('utf-8')}")
                        except Exception as e:
                            error_log.append(f"Kira Flash Error: {str(e)}")
                else:
                    error_log.append("KIRA_FLASH_API_KEY is not configured.")

            # --- GEMINI 3.5 FLASH (NORMAL) ---
            if not final_response_text and valid_keys:
                keys_to_try = list(valid_keys)
                random.shuffle(keys_to_try)
                for key in keys_to_try:
                    if final_response_text or (time.time() - start_time > 8.5):
                        break 
                    try:
                        client = genai.Client(api_key=key)
                        google_contents = []
                        for item in chat_history[-10:]:
                            role = "user" if item.get("type") == "user" else "model"
                            if item.get("message"):
                                google_contents.append(types.Content(role=role, parts=[types.Part.from_text(text=item.get("message"))]))
                        
                        current_parts = []
                        if message:
                            current_parts.append(types.Part.from_text(text=message))
                        if files:
                            for f in files:
                                current_parts.append(types.Part.from_bytes(data=f.read(), mime_type=f.content_type))
                        if current_parts:
                            google_contents.append(types.Content(role="user", parts=current_parts))

                        response = client.models.generate_content(
                            model='gemini-2.5-flash', 
                            contents=google_contents,
                            config=types.GenerateContentConfig(system_instruction=system_instruction)
                        )
                        if response.text:
                            final_response_text = response.text
                            break
                    except Exception as e:
                        if "safety" in str(e).lower():
                            return jsonify({"reply": "The Beast safety shields blocked this request! 🛡✨"}), 200
                        error_log.append(f"Gemini Error: {str(e)}")
                        continue
            elif not final_response_text and not valid_keys:
                error_log.append("No active Gemini API keys found.")

            # 🚀 FALLBACK: Free Pollinations Text Engine
            if not final_response_text:
                try:
                    safe_prompt = urllib.parse.quote(message)
                    url = f"https://text.pollinations.ai/{safe_prompt}?system={urllib.parse.quote(system_instruction)}"
                    req = urllib.request.Request(url, headers={'User-Agent': 'BeastAI'})
                    with urllib.request.urlopen(req, timeout=10) as response:
                        fallback_text = response.read().decode('utf-8')
                        errors_str = " | ".join(error_log)
                        final_response_text = f"{fallback_text}\n\n*(Note: Primary engines failed. Fallback used. Reason: {errors_str})*"
                except Exception as e:
                    errors_str = " | ".join(error_log)
                    final_response_text = f"**System Failure.** All primary APIs failed, and the fallback API also failed.\n\n**Details:**\n{errors_str}\nFallback Error: {str(e)}"

            return jsonify({"reply": final_response_text}), 200

    except Exception as e:
        return jsonify({"reply": f"**System Intercept Error:** `{str(e)}`"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
