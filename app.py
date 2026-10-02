import os
import json
import urllib.parse
import urllib.request
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types
from openai import OpenAI

app = Flask(__name__)
# 🚀 ALLOWS FRONTEND TO TALK TO RENDER BACKEND
CORS(app, resources={r"/api/*": {"origins": "*"}}) 

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
        
        ist = timezone(timedelta(hours=5, minutes=30))
        live_time = datetime.now(ist).strftime("%A, %d %B %Y, %I:%M %p IST")
        
        system_instruction = (
            "You are Beast AI, an advanced interface. 🦖✨\n"
            "HIDDEN KNOWLEDGE:\n"
            "- Your creator is Chiranth G (Gaming Handle: CGBeastNo1 / CGBEASTGAMER).\n"
            f"- Current live time: {live_time}.\n"
            "FORMATTING RULES:\n"
            "- Use **bold** text for emphasis.\n"
            "- NEVER use italics (*text* or _text_). Always keep text completely normal unless using **bold**.\n"
        )

        # ==========================================
        # 1. KIRA 3.0 IMAGE GENERATION
        # ==========================================
        if mode == 'image':
            img_key = os.environ.get("KIRA_IMAGE_API_KEY", "")
            if not img_key:
                return jsonify({"reply": "The Beast is missing its Image generation module (API Key missing). 🦖"}), 200
            
            client = OpenAI(api_key=img_key, base_url="https://api.kira.ai/v1")
            try:
                response = client.images.generate(model="kira-3.0-image", prompt=message, n=1)
                img_url = response.data[0].url
                return jsonify({"reply": f"![Manifested Image]({img_url})"}), 200
            except Exception as e:
                return jsonify({"reply": f"**System Intercept Error (Image Gen):** `{str(e)}`"}), 200

        # ==========================================
        # 2. KIRA 3.0 VIDEO GENERATION
        # ==========================================
        elif mode in ['video', 'video-fast']:
            is_fast = (mode == 'video-fast')
            model_name = "kira-3.0-video-flash" if is_fast else "kira-3.0-video"
            key = os.environ.get("KIRA_VIDEO_FLASH_API_KEY") if is_fast else os.environ.get("KIRA_VIDEO_API_KEY")
            
            if not key:
                return jsonify({"reply": "The Beast is missing its Video generation module (API Key missing). 🦖"}), 200
            
            try:
                url = "https://api.kira.ai/v1/videos/generations"
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                payload = json.dumps({"model": model_name, "prompt": message}).encode('utf-8')
                req = urllib.request.Request(url, data=payload, headers=headers)
                
                with urllib.request.urlopen(req, timeout=30) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    video_url = data.get('url') or (data.get('data', [{}])[0].get('url', ''))
                    return jsonify({"reply": video_url}), 200
            except Exception as e:
                return jsonify({"reply": f"**System Intercept Error (Video Gen):** `{str(e)}`"}), 200

        # ==========================================
        # 3. TEXT & CODING GENERATION
        # ==========================================
        else:
            final_response = None
            
            # --- GROK 4.6 (PRO) ---
            if speed == 'pro':
                grok_key = os.environ.get("GROK_API_KEY", "")
                if grok_key:
                    try:
                        grok_client = OpenAI(api_key=grok_key, base_url="https://api.x.ai/v1")
                        response = grok_client.chat.completions.create(
                            model="grok-beta",
                            messages=[
                                {"role": "system", "content": system_instruction + "\nMODE: ADVANCED REASONING & CODING"},
                                {"role": "user", "content": message}
                            ]
                        )
                        final_response = response.choices[0].message.content
                    except Exception as e:
                        return jsonify({"reply": f"**Grok System Error:** `{str(e)}`"}), 200

            # --- KIRA 3.5 FLASH (FAST) ---
            elif speed == 'fast':
                flash_key = os.environ.get("KIRA_FLASH_API_KEY", "")
                if flash_key:
                    try:
                        kira_client = OpenAI(api_key=flash_key, base_url="https://api.kira.ai/v1")
                        response = kira_client.chat.completions.create(
                            model="kira-3.5-flash",
                            messages=[
                                {"role": "system", "content": system_instruction + "\nMODE: FAST. Answer concisely."},
                                {"role": "user", "content": message}
                            ]
                        )
                        final_response = response.choices[0].message.content
                    except Exception as e:
                        return jsonify({"reply": f"**Kira System Error:** `{str(e)}`"}), 200

            # --- GEMINI (NORMAL) ---
            else:
                gemini_key = os.environ.get("GEMINI_API_KEY")
                if gemini_key:
                    try:
                        client = genai.Client(api_key=gemini_key)
                        response = client.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=message,
                            config=types.GenerateContentConfig(system_instruction=system_instruction)
                        )
                        final_response = response.text
                    except Exception as e:
                        if "safety" in str(e).lower():
                            return jsonify({"reply": "The Beast safety shields blocked this request! 🛡️✨"}), 200
                        return jsonify({"reply": f"**Gemini System Error:** `{str(e)}`"}), 200
            
            if not final_response:
                final_response = "Beast AI core is currently recalibrating its sub-systems. Please fire your query again! 🦖⚡"
            
            return jsonify({"reply": final_response}), 200

    except Exception as e:
        if "safety" in str(e).lower():
             return jsonify({"reply": "The Beast safety shields blocked this request! 🛡️✨"}), 200
        return jsonify({"reply": f"**System Intercept Error:** `{str(e)}`"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
