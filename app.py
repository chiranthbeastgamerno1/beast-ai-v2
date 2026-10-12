import os
os.environ["BEAST_DEPLOYMENT_TARGET"] = "render"
"""Beast AI backend shared by the Vercel and Render deployments.

API keys are read only from environment variables. Never commit secret values.
"""
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:  # Keep health/debug endpoints available if dependency installation fails.
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    GENAI_AVAILABLE = False

APP_TARGET = os.environ.get("BEAST_DEPLOYMENT_TARGET", "render").strip().lower()
IS_VERCEL = APP_TARGET == "vercel"
MAX_REQUEST_BYTES = 4_300_000 if IS_VERCEL else 20 * 1024 * 1024
GEMINI_TIMEOUT_MS = 12_000 if IS_VERCEL else 25_000
GEMINI_BUDGET_SECONDS = 40 if IS_VERCEL else 90
VIDEO_TIMEOUT_SECONDS = 45 if IS_VERCEL else 75
KIRA_API_BASE_URL = os.environ.get("KIRA_API_BASE_URL", "https://kiraai.vn/api/v1").rstrip("/")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4.6").strip()
KIRA_FLASH_MODEL = os.environ.get("KIRA_FLASH_MODEL", "kira-3.5-flash").strip()
KIRA_PRO_MODEL = os.environ.get("KIRA_PRO_MODEL", "kira-3.5-pro").strip()
KIRA_IMAGE_MODEL = os.environ.get("KIRA_IMAGE_MODEL", "kira-3.0-image").strip()
KIRA_VIDEO_MODEL = os.environ.get("KIRA_VIDEO_MODEL", "kira-3.0-video").strip()
KIRA_VIDEO_FLASH_MODEL = os.environ.get("KIRA_VIDEO_FLASH_MODEL", "kira-3.0-video-flash").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
CORS(app, resources={r"/api/*": {"origins": "*"}})


def configured_gemini_keys() -> list[str]:
    return [
        value.strip()
        for name in ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(1, 10)]
        if (value := os.environ.get(name)) and value.strip()
    ]


def _json_request_body() -> dict[str, Any]:
    if request.is_json:
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}
    return {}


def _http_request_json(url: str, api_key: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        content = response.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Provider returned non-JSON response (HTTP {response.status}).") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Provider returned an unexpected JSON response.")
        return parsed


def _provider_error(exc: Exception, provider: str) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
        except Exception:
            detail = "No error body was provided."
        return f"{provider} returned HTTP {exc.code}: {detail}"
    if isinstance(exc, urllib.error.URLError):
        return f"{provider} network error: {exc.reason}"
    return f"{provider} error: {str(exc)[:1200]}"


def _find_media_url(payload: Any) -> str | None:
    """Handle common direct and nested URL shapes without pretending job IDs are files."""
    if isinstance(payload, str):
        value = payload.strip()
        return value if value.startswith(("https://", "http://")) else None
    if isinstance(payload, list):
        for item in payload:
            result = _find_media_url(item)
            if result:
                return result
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("url", "image_url", "video_url", "output_url", "download_url", "uri"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip().startswith(("https://", "http://")):
            return value.strip()
    for key in ("data", "images", "videos", "output", "outputs", "result", "results", "artifacts"):
        if key in payload:
            result = _find_media_url(payload[key])
            if result:
                return result
    return None


def _media_response(media_type: str, url: str, provider: str, note: str = ""):
    return jsonify({
        "reply": url,
        "media_type": media_type,
        "media_url": url,
        "provider": provider,
        "note": note,
    }), 200


def _make_image(message: str):
    key = os.environ.get("KIRA_IMAGE_API_KEY", "").strip()
    errors: list[str] = []
    if key:
        try:
            result = _http_request_json(
                f"{KIRA_API_BASE_URL}/images/generations",
                key,
                {"model": KIRA_IMAGE_MODEL, "prompt": message, "n": 1},
                timeout=25 if IS_VERCEL else 45,
            )
            url = _find_media_url(result)
            if url:
                return _media_response("image", url, "Kira")
            errors.append("Kira returned JSON but no direct image URL.")
        except Exception as exc:
            errors.append(_provider_error(exc, "Kira image API"))
    else:
        errors.append("KIRA_IMAGE_API_KEY is not configured.")

    # Fallback is a third-party URL generator, not a promise of unlimited service.
    seed = random.randint(1, 2_147_483_647)
    encoded_prompt = urllib.parse.quote(message, safe="")
    fallback_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?nologo=true&seed={seed}"
    note = "Kira image generation was unavailable; using the external Pollinations fallback. " + " ".join(errors)
    return _media_response("image", fallback_url, "Pollinations fallback", note)


def _make_video(message: str, fast: bool):
    env_name = "KIRA_VIDEO_FLASH_API_KEY" if fast else "KIRA_VIDEO_API_KEY"
    key = os.environ.get(env_name, "").strip()
    model = KIRA_VIDEO_FLASH_MODEL if fast else KIRA_VIDEO_MODEL
    if not key:
        return jsonify({
            "reply": f"Video generation is not configured: add {env_name} in the hosting environment.",
            "error": True,
        }), 200
    try:
        result = _http_request_json(
            f"{KIRA_API_BASE_URL}/videos/generations",
            key,
            {"model": model, "prompt": message},
            timeout=VIDEO_TIMEOUT_SECONDS,
        )
        url = _find_media_url(result)
        if url:
            return _media_response("video", url, "Kira")
        # Some video services return an async task instead of an immediate URL. Don't invent a polling URL.
        possible_id = result.get("id") or result.get("task_id") or result.get("job_id")
        status = result.get("status") or result.get("state")
        detail = "The provider did not return a playable video URL."
        if possible_id:
            detail += f" It returned job ID {str(possible_id)[:160]}"
            if status:
                detail += f" (status: {str(status)[:80]})"
            detail += ". This provider's documented job-status endpoint is needed to fetch the finished video."
        else:
            detail += " Check Kira's API response format and model access."
        return jsonify({"reply": detail, "error": True, "provider_response_fields": list(result.keys())[:20]}), 200
    except Exception as exc:
        return jsonify({"reply": _provider_error(exc, "Kira video API"), "error": True}), 200


def _chat_history(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    history: list[dict[str, str]] = []
    for item in parsed[-10:]:
        if not isinstance(item, dict):
            continue
        text = item.get("message")
        if not isinstance(text, str) or not text.strip():
            continue
        role = "user" if item.get("type") == "user" else "assistant"
        history.append({"role": role, "text": text[:12000]})
    return history


def _openai_compatible_chat(api_url: str, api_key: str, model: str, history: list[dict[str, str]], message: str, system_prompt: str, timeout: int) -> str:
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for item in history:
        messages.append({"role": item["role"], "content": item["text"]})
    if message:
        messages.append({"role": "user", "content": message})
    result = _http_request_json(
        api_url,
        api_key,
        {"model": model, "messages": messages},
        timeout=timeout,
    )
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Provider response did not include chat choices.")
    choice = choices[0]
    content = choice.get("message", {}).get("content") if isinstance(choice, dict) else None
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Provider returned an empty assistant message.")
    return content.strip()


def _generate_with_gemini(keys: list[str], message: str, history: list[dict[str, str]], files: list[dict[str, Any]], system_prompt: str, started: float) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    if not GENAI_AVAILABLE:
        return None, ["Python dependency google-genai is not installed. Re-deploy after installing requirements.txt."]
    if not keys:
        return None, ["No Gemini API key is configured. Add GEMINI_API_KEY (or GEMINI_API_KEY_1 through _9)."]

    keys_to_try = keys[:]
    random.shuffle(keys_to_try)
    for key in keys_to_try:
        if time.time() - started > GEMINI_BUDGET_SECONDS:
            errors.append("Gemini key retries stopped to stay within the deployment request timeout.")
            break
        try:
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
            contents = []
            for item in history:
                role = "user" if item["role"] == "user" else "model"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=item["text"])]))
            parts = []
            if message:
                parts.append(types.Part.from_text(text=message))
            for uploaded in files:
                parts.append(types.Part.from_bytes(data=uploaded["bytes"], mime_type=uploaded["mime_type"]))
            if parts:
                contents.append(types.Content(role="user", parts=parts))
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt),
            )
            text = response.text
            if isinstance(text, str) and text.strip():
                return text.strip(), errors
            errors.append(f"Gemini model {GEMINI_MODEL} returned no text.")
        except Exception as exc:
            description = str(exc)
            if "safety" in description.lower():
                return "The request was blocked by Gemini's safety filters.", errors
            errors.append(f"Gemini: {description[:600]}")
    return None, errors


@app.route("/", methods=["GET"])
def home():
    return "Beast AI Core is online. Use /api/health for status."


@app.route("/api/health", methods=["GET"])
def health_check():
    keys = configured_gemini_keys()
    return jsonify({
        "status": "online",
        "target": APP_TARGET,
        "genai_installed": GENAI_AVAILABLE,
        "gemini_keys_configured": len(keys),
        "models": {
            "gemini": GEMINI_MODEL,
            "grok": GROK_MODEL,
            "kira_fast": KIRA_FLASH_MODEL,
            "kira_pro": KIRA_PRO_MODEL,
            "kira_image": KIRA_IMAGE_MODEL,
            "kira_video": KIRA_VIDEO_MODEL,
            "kira_video_fast": KIRA_VIDEO_FLASH_MODEL,
        },
        "features": {
            "chat": True,
            "attachments": GENAI_AVAILABLE and bool(keys),
            "image_generation": True,  # Kira primary with an external fallback URL
            "video_generation": bool(os.environ.get("KIRA_VIDEO_API_KEY")),
            "video_generation_fast": bool(os.environ.get("KIRA_VIDEO_FLASH_API_KEY")),
            "kira_pro_chat": bool(os.environ.get("KIRA_PRO_API_KEY")),
        },
    }), 200


# Compatibility: some old clients pinged /api/chat with GET. Return health instead of 405.
@app.route("/api/chat", methods=["GET"])
def chat_health_compat():
    return health_check()


@app.route("/api/chat", methods=["POST"])
def chat():
    started = time.time()
    try:
        body = _json_request_body()
        message = (request.form.get("message") if request.form else None) or body.get("message", "")
        mode = (request.form.get("mode") if request.form else None) or body.get("mode", "chat")
        speed = (request.form.get("speed") if request.form else None) or body.get("speed", "normal")
        history_raw = (request.form.get("history") if request.form else None) or json.dumps(body.get("history", []))
        message = message.strip() if isinstance(message, str) else ""
        mode = mode if mode in {"chat", "image", "video", "video-fast"} else "chat"
        speed = speed if speed in {"fast", "normal", "pro", "kira-pro"} else "normal"
        history = _chat_history(history_raw)

        files = request.files.getlist("files") if request.files else []
        uploaded_files: list[dict[str, Any]] = []
        for uploaded in files:
            content = uploaded.read()
            if not content:
                continue
            uploaded_files.append({
                "bytes": content,
                "mime_type": uploaded.mimetype or "application/octet-stream",
                "filename": uploaded.filename or "attachment",
            })
        if not message and not uploaded_files:
            return jsonify({"reply": "Type a message or attach a file first."}), 200

        if mode == "image":
            if not message:
                return jsonify({"reply": "Describe the image you want to generate."}), 200
            return _make_image(message)
        if mode in {"video", "video-fast"}:
            if not message:
                return jsonify({"reply": "Describe the video you want to generate."}), 200
            return _make_video(message, fast=(mode == "video-fast"))

        ist = timezone(timedelta(hours=5, minutes=30))
        live_time = datetime.now(ist).strftime("%A, %d %B %Y, %I:%M %p IST")
        system_prompt = (
            "You are Beast AI, a friendly, useful, witty assistant. Be clear and direct.\n"
            f"Current local time: {live_time}.\n"
            "Use Markdown for structure when it helps. Do not claim you generated media unless the API confirms it."
        )
        errors: list[str] = []
        final_text: str | None = None
        used_provider = ""
        gemini_keys = configured_gemini_keys()

        # Only route to the selected third-party chat tier when there are no file attachments.
        if not uploaded_files and speed == "kira-pro":
            kira_pro_key = os.environ.get("KIRA_PRO_API_KEY", "").strip()
            if kira_pro_key:
                try:
                    final_text = _openai_compatible_chat(
                        f"{KIRA_API_BASE_URL}/chat/completions", kira_pro_key, KIRA_PRO_MODEL,
                        history, message, system_prompt, timeout=20 if IS_VERCEL else 35,
                    )
                    used_provider = "Kira Pro"
                except Exception as exc:
                    errors.append(_provider_error(exc, "Kira Pro"))
            else:
                errors.append("KIRA_PRO_API_KEY is not configured; falling back to Gemini.")

        elif not uploaded_files and speed == "pro":
            grok_key = os.environ.get("GROK_API_KEY", "").strip()
            if grok_key:
                try:
                    final_text = _openai_compatible_chat(
                        "https://api.x.ai/v1/chat/completions", grok_key, GROK_MODEL,
                        history, message, system_prompt, timeout=20 if IS_VERCEL else 35,
                    )
                    used_provider = "Grok"
                except Exception as exc:
                    errors.append(_provider_error(exc, "Grok"))
            else:
                errors.append("GROK_API_KEY is not configured; falling back to Gemini.")

        elif not uploaded_files and speed == "fast":
            kira_key = os.environ.get("KIRA_FLASH_API_KEY", "").strip()
            if kira_key:
                try:
                    final_text = _openai_compatible_chat(
                        f"{KIRA_API_BASE_URL}/chat/completions", kira_key, KIRA_FLASH_MODEL,
                        history, message, system_prompt, timeout=20 if IS_VERCEL else 35,
                    )
                    used_provider = "Kira Fast"
                except Exception as exc:
                    errors.append(_provider_error(exc, "Kira Fast"))
            else:
                errors.append("KIRA_FLASH_API_KEY is not configured; falling back to Gemini.")

        if not final_text:
            final_text, gemini_errors = _generate_with_gemini(
                gemini_keys, message, history, uploaded_files, system_prompt, started,
            )
            errors.extend(gemini_errors)
            if final_text:
                used_provider = "Gemini"

        # Last-resort text fallback is used only for text-only requests. It cannot inspect attachments.
        if not final_text and not uploaded_files:
            try:
                encoded_prompt = urllib.parse.quote(message, safe="")
                encoded_system = urllib.parse.quote(system_prompt, safe="")
                fallback_url = f"https://text.pollinations.ai/{encoded_prompt}?system={encoded_system}"
                req = urllib.request.Request(fallback_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=12 if IS_VERCEL else 20) as response:
                    final_text = response.read().decode("utf-8", errors="replace").strip()
                    used_provider = "Pollinations fallback"
            except Exception as exc:
                errors.append(_provider_error(exc, "Text fallback"))

        if not final_text:
            return jsonify({
                "reply": "The request could not be completed by the configured models. Check the hosting environment variables and logs.\n\n" + "\n".join(errors[-5:]),
                "error": True,
            }), 200

        notice = ""
        if used_provider == "Pollinations fallback":
            notice = "The configured primary model was unavailable, so an external fallback answered this message. " + " ".join(errors[-3:])
        return jsonify({"reply": final_text, "provider": used_provider, "notice": notice}), 200

    except Exception as exc:
        app.logger.exception("Unhandled Beast AI request error")
        if isinstance(exc, (ValueError, TypeError)):
            message = str(exc)
        else:
            message = f"Unexpected backend error: {type(exc).__name__}. Check server logs."
        return jsonify({"reply": message, "error": True}), 200


@app.errorhandler(413)
def request_too_large(_error):
    size_mb = round(MAX_REQUEST_BYTES / (1024 * 1024), 1)
    return jsonify({
        "reply": f"Your upload is too large for this deployment. Keep the total request under about {size_mb} MB, or upload fewer/smaller files.",
        "error": True,
    }), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
