import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai

app = Flask(__name__)
# Enable CORS so Cloudflare Pages can talk to this Render server securely
CORS(app)

# Configure Gemini securely from environment variables
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

@app.route('/')
def home():
    # Keep-awake ping route for cron-job.org
    return "Beast AI Backend is awake and running!"

@app.route('/api/chat', methods=['POST'])
def chat():
    message = request.form.get('message', '')
    mode = request.form.get('mode', 'chat')
    
    # 🚀 INTERCEPT THE DROPDOWN SPEED SIGNAL
    speed = request.form.get('speed', 'normal')
    
    # Parse history for memory context
    history_str = request.form.get('history', '[]')
    try:
        history_data = json.loads(history_str)
    except:
        history_data = []

    formatted_history = []
    for msg in history_data:
        role = "user" if msg.get("type") == "user" else "model"
        formatted_history.append({"role": role, "parts": [msg.get("message", "")]})

    # 🚀 SHIFT GEARS: APPLY MODEL AND INSTRUCTIONS BASED ON UI SELECTION
    if speed == 'fast':
        target_model = "gemini-1.5-flash"
        sys_instruct = "You are Beast AI. Answer accurately but as concisely and quickly as possible. Get straight to the point without filler."
    elif speed == 'thinking':
        # Activates heavy reasoning/thinking capabilities
        target_model = "gemini-2.0-flash-thinking-exp-01-21" 
        sys_instruct = "You are Beast AI. Think step-by-step. Analyze the request thoroughly and provide a highly detailed, comprehensive, and structured response."
    else:
        # Default Normal Speed
        target_model = "gemini-1.5-pro"
        sys_instruct = "You are Beast AI. Provide a balanced, highly intelligent, and helpful response."

    try:
        # If Image Generation mode is selected, use standard pro behavior (or image specific API if you have one)
        if mode == 'image':
            # This is a placeholder standard fallback if image logic isn't strictly defined
            sys_instruct = "You are an image generator prompt assistant. Describe this vividly."

        model = genai.GenerativeModel(
            model_name=target_model,
            system_instruction=sys_instruct
        )
        
        chat_session = model.start_chat(history=formatted_history)
        response = chat_session.send_message(message)
        
        return jsonify({"reply": response.text})
    except Exception as e:
        return jsonify({"reply": f"Error communicating with Beast servers: {str(e)}"})

# Required for Render to bind to the correct port
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
