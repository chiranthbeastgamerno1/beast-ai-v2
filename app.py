import os
import json
import urllib.parse
import PIL.Image
from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai

app = Flask(__name__)
# Enable CORS so Cloudflare Pages can talk to this Render server securely
CORS(app)

# 🔑 API KEY CONFIGURATION
# Set GEMINI_API_KEY in your Render Environment Variables!
API_KEY = os.environ.get("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

@app.route('/')
def home():
    # ⏱️ Keep-awake ping route for cron-job.org to prevent Render cold starts
    return "Beast AI Backend is awake and running!"

@app.route('/api/chat', methods=['POST'])
def chat():
    message = request.form.get('message', '')
    mode = request.form.get('mode', 'chat')
    speed = request.form.get('speed', 'normal')
    
    # 📂 1. PROCESS ATTACHED FILES (BEAST VISION)
    uploaded_files = request.files.getlist('files')
    image_parts = []
    for file in uploaded_files:
        try:
            # Read the file directly into a PIL Image so Gemini can "see" it
            img = PIL.Image.open(file)
            image_parts.append(img)
        except Exception as e:
            print(f"Error processing image: {e}")

    # 🖼️ 2. MANIFEST IMAGE MODE
    if mode == 'image':
        try:
            # Step A: Use the Fast model to enhance your basic idea into a master prompt
            prompt_model = genai.GenerativeModel("gemini-1.5-flash")
            enhanced_prompt = prompt_model.generate_content(
                f"You are an expert cinematic image prompt engineer. Take this basic idea and turn it into a highly detailed, stunning text-to-image prompt. Reply with ONLY the prompt, no intro text. Idea: {message}"
            ).text
            
            # Step B: Encode and send to a free, fast image generator
            safe_prompt = urllib.parse.quote(enhanced_prompt.strip())
            
            import random
            seed = random.randint(1, 100000)
            
            image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&nologo=true&seed={seed}"
            
            return jsonify({"reply": image_url})
        except Exception as e:
            return jsonify({"reply": f"Manifestation failed: {str(e)}"})

    # 🧠 3. PARSE CHAT HISTORY MEMORY
    history_str = request.form.get('history', '[]')
    try:
        history_data = json.loads(history_str)
    except:
        history_data = []

    formatted_history = []
    for msg in history_data:
        role = "user" if msg.get("type") == "user" else "model"
        formatted_history.append({"role": role, "parts": [msg.get("message", "")]})

    # 🚀 4. SHIFT GEARS (SPEED SELECTOR)
    if speed == 'fast':
        target_model = "gemini-1.5-flash"
        sys_instruct = "You are Beast AI. Answer accurately but as concisely and quickly as possible. Get straight to the point without filler."
    elif speed == 'thinking':
        target_model = "gemini-2.0-flash-thinking-exp-01-21" 
        sys_instruct = "You are Beast AI. Think step-by-step. Analyze the request thoroughly and provide a highly detailed, comprehensive, and structured response."
    else:
        target_model = "gemini-1.5-pro"
        sys_instruct = "You are Beast AI. Provide a balanced, highly intelligent, and helpful response."

    # 🤖 5. GENERATE FINAL RESPONSE
    try:
        model = genai.GenerativeModel(
            model_name=target_model,
            system_instruction=sys_instruct
        )
        
        chat_session = model.start_chat(history=formatted_history)
        
        # Combine your text message with any images you attached
        content_to_send = [message] + image_parts if image_parts else message
        
        response = chat_session.send_message(content_to_send)
        
        return jsonify({"reply": response.text})
    except Exception as e:
        return jsonify({"reply": f"Error communicating with Beast servers: {str(e)}"})

# Required for Render to bind to the correct port
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
