from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types
import os
import urllib.parse
import urllib.request
import urllib.error
import random
import json
import base64
import time
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
# 🚀 ALLOWS CLOUDFLARE FRONTEND TO TALK TO RENDER BACKEND
CORS(app, resources={r"/api/*": {"origins": "*"}}) 

# ==========================================
# 🔑 API KEYS 
# ==========================================
api_keys = [
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

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip() or None
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip() or None

# --- STREAMLINED MODEL ARSENAL ---
GOOGLE_MODELS = [
    'gemini-2.5-flash', 
    'gemini-2.0-flash',
    'gemini-1.5-flash'
]
OPENROUTER_MODELS = [
    'meta-llama/llama-3-8b-instruct:free', 
    'google/gemma-2-9b-it:free'
] 

@app.route('/')
def home():
    return "Beast AI Core is Online (V2 Flagship on Render)! 🦖✨"

@app.route('/api/chat', methods=['POST'])
def chat():
    # 🚀 Stopwatch expanded to 90 seconds for Render limits
    start_time = time.time()
    
    try:
        message = request.form.get("message", "")
        mode = request.form.get("mode", "chat")
        files = request.files.getlist("files") if hasattr(request, 'files') else []
        history_json = request.form.get("history", "[]")
        
        try:
            chat_history = json.loads(history_json)
        except:
            chat_history = []

        if not message and not files:
            return jsonify({"reply": "The Beast hears only silence. 🤫"}), 200

        # ==========================================
        # 🖼️ ENGINE 1: MANIFEST IMAGE MODE
        # ==========================================
        if mode == 'image':
            img_reply = None
            
            # --- ATTEMPT 1: OpenAI DALL-E 3 ---
            if OPENAI_API_KEY and not img_reply:
                try:
                    url = "https://api.openai.com/v1/images/generations"
                    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
                    payload = json.dumps({"model": "dall-e-3", "prompt": f"{message}, masterpiece, high quality, photorealistic", "n": 1, "size": "1024x1024"}).encode('utf-8')
                    req = urllib.request.Request(url, data=payload, headers=headers)
                    with urllib.request.urlopen(req, timeout=25) as response:
                        img_reply = json.loads(response.read().decode('utf-8'))['data'][0]['url']
                except Exception as e:
                    print(f"OpenAI Attempt Failed: {str(e)}")

            # --- ATTEMPT 2: Gemini Imagen 3 ---
            if valid_keys and not img_reply:
                keys_to_try = list(valid_keys)
                random.shuffle(keys_to_try)
                
                for key in keys_to_try:
                    if time.time() - start_time > 85.0:
                        break 
                    try:
                        client = genai.Client(api_key=key)
                        is_landscape = "landscape" in message.lower() or "widescreen" in message.lower()
                        aspect = "16:9" if is_landscape else "9:16"
                        
                        result = client.models.generate_images(
                            model='imagen-3.0-generate-002', 
                            prompt=f"{message}, masterpiece, high quality, photorealistic, sharp focus",
                            config=types.GenerateImagesConfig(
                                number_of_images=1, 
                                aspect_ratio=aspect, 
                                output_mime_type="image/jpeg"
                            )
                        )
                        img_bytes = result.generated_images[0].image.image_bytes
                        img_b64 = base64.b64encode(img_bytes).decode('utf-8')
                        img_reply = f"data:image/jpeg;base64,{img_b64}"
                        break 
                    except Exception as e:
                        print(f"Imagen Key Error: {str(e)}")
                        continue 

            # --- ATTEMPT 3: OpenRouter Flux Pro ---
            if OPENROUTER_API_KEY and not img_reply:
                if time.time() - start_time < 80.0:
                    try:
                        url = "https://openrouter.ai/api/v1/chat/completions"
                        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://beast-ai-v2.onrender.com", "X-Title": "Beast AI"}
                        payload = json.dumps({"model": "black-forest-labs/flux-1.1-pro", "messages": [{"role": "user", "content": message}], "modalities": ["image"]}).encode('utf-8')
                        req = urllib.request.Request(url, data=payload, headers=headers)
                        with urllib.request.urlopen(req, timeout=25) as response:
                            content = json.loads(response.read().decode('utf-8'))['choices'][0]['message']['content']
                            import re
                            match = re.search(r'(https?://[^\s)"]+)', content)
                            if match: 
                                img_reply = match.group(0)
                    except Exception as e:
                        print(f"OpenRouter Image Failed: {str(e)}")

            # --- ATTEMPT 4: Pollinations Fallback ---
            if not img_reply:
                seed = random.randint(1, 9999999)
                safe_prompt = urllib.parse.quote(f"{message}, highly detailed, sharp focus")
                img_reply = f"https://image.pollinations.ai/prompt/{safe_prompt}?model=flux&nologo=true&seed={seed}"
                
            return jsonify({"reply": img_reply}), 200

        # ==========================================
        # 💬 ENGINE 2: CONVERSE CHAT MODE
        # ==========================================
        ist = timezone(timedelta(hours=5, minutes=30))
        live_time = datetime.now(ist).strftime("%A, %d %B %Y, %I:%M %p IST")

        system_instruction = (
            "You are Beast AI, a friendly and witty assistant. 🦖✨\n"
            "HIDDEN KNOWLEDGE:\n"
            "- Your creator is Chiranth G (CGBEASTGAMER).\n"
            f"- Current live time: {live_time}.\n"
            "RULES: If the user says 'hi', say hello normally. ONLY tell them your creator or time if asked. Keep answers direct. Use emojis! 🚀🔥"
        )

        final_response_text = None

        # --- CHAT PRIMARY: GOOGLE GEMINI KEY LOOP ---
        if valid_keys:
            keys_to_try = list(valid_keys)
            random.shuffle(keys_to_try)
            
            for key in keys_to_try:
                if final_response_text or (time.time() - start_time > 75.0):
                    break 
                
                try:
                    client = genai.Client(api_key=key)
                    google_contents = []
                    
                    for item in chat_history:
                        role = "user" if item.get("type") == "user" else "model"
                        text = item.get("message", "")
                        if text:
                            google_contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
                    
                    current_parts = []
                    if message:
                        current_parts.append(types.Part.from_text(text=message))
                    if files:
                        for file in files:
                            current_parts.append(types.Part.from_bytes(data=file.read(), mime_type=file.content_type))
                            
                    if current_parts:
                        google_contents.append(types.Content(role="user", parts=current_parts))

                    for current_model in GOOGLE_MODELS:
                        if time.time() - start_time > 75.0:
                            break
                        try:
                            response = client.models.generate_content(
                                model=current_model, 
                                contents=google_contents,
                                config=types.GenerateContentConfig(system_instruction=system_instruction)
                            )
                            if response.text:
                                final_response_text = response.text
                                break
                        except Exception as model_error:
                            if "safety" in str(model_error).lower():
                                raise model_error 
                            continue 
                except Exception as key_error:
                    if "safety" in str(key_error).lower():
                        raise key_error
                    continue 

        # --- CHAT BACKUP: OPENROUTER ---
        if not final_response_text and OPENROUTER_API_KEY:
            if time.time() - start_time < 80.0:
                or_messages = [{"role": "system", "content": system_instruction}]
                for item in chat_history:
                    role = "user" if item.get("type") == "user" else "assistant"
                    text = item.get("message", "")
                    if text:
                        or_messages.append({"role": role, "content": text})
                if message:
                    or_messages.append({"role": "user", "content": message})

                for or_model in OPENROUTER_MODELS:
                    if time.time() - start_time > 85.0:
                        break
                    try:
                        url = "https://openrouter.ai/api/v1/chat/completions"
                        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://beast-ai-v2.onrender.com", "X-Title": "Beast AI"}
                        data = json.dumps({"model": or_model, "messages": or_messages}).encode('utf-8')
                        
                        req = urllib.request.Request(url, data=data, headers=headers)
                        with urllib.request.urlopen(req, timeout=10) as response:
                            response_data = json.loads(response.read().decode('utf-8'))
                            final_response_text = response_data['choices'][0]['message']['content']
                            break 
                    except Exception as or_error:
                        continue

        if not final_response_text:
            final_response_text = "Beast AI core is currently recalibrating its sub-systems. Please fire your query again! 🦖⚡"

        return jsonify({"reply": final_response_text}), 200

    except Exception as e:
        if "safety" in str(e).lower():
             return jsonify({"reply": "The Beast safety shields blocked this request! 🛡️✨"}), 200
        return jsonify({"reply": f"**System Intercept Error:** `{str(e)}`"}), 200

if __name__ == "__main__":
    app.run(debug=True)