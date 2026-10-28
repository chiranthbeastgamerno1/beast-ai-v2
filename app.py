import os
os.environ["BEAST_DEPLOYMENT_TARGET"] = "render"
"""Beast AI backend shared by the Vercel and Render deployments.

API keys are read only from environment variables. Never commit secret values.
"""
import base64
import html
import json
import os
import random
import re
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
# Text requests have a browser-side 55s deadline. Keep each provider attempt bounded
# and reserve time for model fallback so failed providers do not leave the UI waiting.
GEMINI_TIMEOUT_MS = 6_000
GEMINI_BUDGET_SECONDS = 25
VIDEO_TIMEOUT_SECONDS = 45 if IS_VERCEL else 75
KIRA_API_BASE_URL = os.environ.get("KIRA_API_BASE_URL", "https://kiraai.vn/api/v1").rstrip("/")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4.6").strip()
KIRA_FLASH_MODEL = os.environ.get("KIRA_FLASH_MODEL", "kira-3.5-flash").strip()
KIRA_PRO_MODEL = os.environ.get("KIRA_PRO_MODEL", "kira-3.5-pro").strip()

GEMINI_LIVE_MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live").strip() or "gemini-3.8-live"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_WHISPER_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo").strip() or "whisper-large-v3-turbo"
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
LIVE_SYSTEM_INSTRUCTION = (
    "You are Beast AI, a friendly, helpful real-time voice assistant. "
    "Beast AI was created by Chiranth when he was in 8th grade. If asked who created you, answer simply and accurately. "
    "Speak naturally, keep answers concise, allow the user to interrupt, and never claim to have performed an action you did not perform."
)

# Images use Cloudflare Workers AI (FLUX.1 Schnell) so image generation can
# use Cloudflare's free daily allocation instead of fal.ai credits.
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
CLOUDFLARE_IMAGE_MODEL = os.environ.get(
    "CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"
).strip()
# Video generation uses API Pass exclusively. Keep this key in deployment secrets,
# never in index.html or any client-side JavaScript.
APIPASS_API_KEY = os.environ.get("APIPASS_API_KEY", "").strip()
APIPASS_BASE_URL = os.environ.get("APIPASS_BASE_URL", "https://api.apipass.dev/api/v1").strip().rstrip("/")
APIPASS_CHANNEL = os.environ.get("APIPASS_CHANNEL", "auto").strip().lower()
if APIPASS_CHANNEL not in {"auto", "starter", "regular", "official"}:
    APIPASS_CHANNEL = "auto"

APIPASS_VEO_QUALITY_MODEL = "google/veo-3-1-quality"
APIPASS_VEO_FAST_MODEL = "google/veo-3-1-fast"
APIPASS_VIDEO_RESOLUTION = "1080p"
# Veo 3.1 requires 8 seconds when requesting 1080p output.
APIPASS_VIDEO_DURATION = "8"


def _apipass_http_json(path: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 25) -> dict[str, Any]:
    """Call API Pass with a server-side Bearer token."""
    if not APIPASS_API_KEY:
        raise RuntimeError("APIPASS_API_KEY is missing from this deployment's environment variables. 🔑")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {APIPASS_API_KEY}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    url = APIPASS_BASE_URL + "/" + path.lstrip("/")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"API Pass returned a non-JSON response (HTTP {response.status}).") from exc
    if not isinstance(parsed, dict):
        raise ValueError("API Pass returned an unexpected response format.")
    api_code = parsed.get("code")
    if api_code not in (None, 200, "200") or parsed.get("success") is False:
        detail = parsed.get("message") or parsed.get("error") or "request rejected"
        raise RuntimeError(f"API Pass rejected the request ({api_code}): {str(detail)[:900]}")
    return parsed


def _start_apipass_video(prompt: str, mode: str):
    """Submit a 1080p, 8-second Veo 3.1 quality or Fast job."""
    if not APIPASS_API_KEY:
        return jsonify({
            "reply": "Video generation is not configured yet. Add APIPASS_API_KEY to the hosting environment and redeploy. 🔑",
            "error": True,
        }), 200
    clean_prompt = prompt.strip()[:4000]
    if not clean_prompt:
        return jsonify({"reply": "Describe the video you want to create. 🎬", "error": True}), 200

    fast_mode = mode == "video-fast"
    model = APIPASS_VEO_FAST_MODEL if fast_mode else APIPASS_VEO_QUALITY_MODEL
    payload = {
        "model": model,
        "channel": APIPASS_CHANNEL,
        "input": {
            "prompt": clean_prompt,
            "aspect_ratio": "LANDSCAPE",
            "video_generate_type": "text_to_video",
            "duration": APIPASS_VIDEO_DURATION,
            "resolution": APIPASS_VIDEO_RESOLUTION,
        },
    }
    try:
        submitted = _apipass_http_json("jobs/createTask", method="POST", payload=payload, timeout=30)
        data = submitted.get("data") if isinstance(submitted.get("data"), dict) else {}
        task_id = data.get("taskId") or submitted.get("taskId")
        if not isinstance(task_id, str) or not task_id.strip():
            raise RuntimeError("API Pass did not return a taskId. Check the API Pass response and account credits.")
        label = "Veo 3.1 Fast" if fast_mode else "Veo 3.1 Quality"
        return jsonify({
            "media_pending": True,
            "request_id": task_id,
            "provider": "APIPASS",
            "mode": mode,
            "media_type": "video",
            "reply": f"Generating with Google {label} at 1080p (8 seconds). This can take a few minutes… 🎬",
        }), 202
    except Exception as exc:
        app.logger.exception("API Pass video submission failed")
        detail = _provider_error(exc, "API Pass") if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError)) else str(exc)
        return jsonify({"reply": f"Video generation could not start: {detail[:900]} ⚠️", "error": True}), 200


def _poll_apipass_video(task_id: str):
    """Poll API Pass and return the generated MP4 URL when the task finishes."""
    if not APIPASS_API_KEY:
        return jsonify({"reply": "APIPASS_API_KEY is missing from the hosting environment. 🔑", "error": True}), 200
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", task_id):
        return jsonify({"reply": "The API Pass task ID is invalid. Start a new video generation. ⚠️", "error": True}), 400
    try:
        encoded_id = urllib.parse.quote(task_id, safe="")
        response_data = _apipass_http_json(f"jobs/recordInfo?taskId={encoded_id}", method="GET", timeout=25)
        data = response_data.get("data") if isinstance(response_data.get("data"), dict) else response_data
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        result_obj = data.get("resultJson") or data.get("result") or data.get("output")
        if isinstance(result_obj, str):
            try:
                result_obj = json.loads(result_obj)
            except json.JSONDecodeError:
                result_obj = {"url": result_obj} if result_obj.startswith(("https://", "http://")) else {}

        if state in {"success", "succeeded", "completed", "complete", "done"}:
            video_url = _find_media_url(result_obj) or _find_media_url(data.get("resultUrls")) or _find_media_url(data)
            if not isinstance(video_url, str) or not video_url.startswith("https://"):
                return jsonify({"reply": "API Pass marked this job complete but did not return a secure video URL. ⚠️", "error": True}), 200
            return _media_response("video", video_url, "API Pass · Google Veo 3.1", "🎬 Generated with Google Veo 3.1 at 1080p, 8 seconds.")

        if state in {"fail", "failed", "failure", "error", "cancelled", "canceled"}:
            detail = data.get("failMsg") or data.get("message") or data.get("failCode") or "The provider could not complete this video."
            return jsonify({"reply": f"API Pass video generation failed: {str(detail)[:900]} ⚠️", "error": True}), 200

        return jsonify({
            "media_pending": True,
            "request_id": task_id,
            "provider": "APIPASS",
            "mode": "video",
            "media_type": "video",
            "queue_status": state or "processing",
            "reply": "Google Veo 3.1 is generating your 1080p video… 🎬",
        }), 200
    except Exception as exc:
        detail = _provider_error(exc, "API Pass status") if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError)) else str(exc)
        return jsonify({"reply": f"Could not check API Pass video status: {detail[:900]} ⚠️", "error": True}), 200



USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
CORS(app, resources={r"/api/*": {"origins": "*"}})


# Beast AI's creator identity and emoji policy are enforced server-side so they
# apply regardless of which configured text model handles a request.
CREATOR_REPLY = "Beast AI was created by Chiranth when he was in 8th grade! 🚀"

_EMOJI_PATTERN = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")
_CREATOR_QUESTION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bwho\s+(?:actually\s+)?(?:made|created|built|developed|designed|invented)\s+(?:you|u|beast(?:\s*ai)?|this\s+ai)\b",
    r"\bwho\s+(?:is|'s)\s+your\s+(?:creator|maker|developer|founder|builder|author|owner)\b",
    r"\b(?:your|beast\s*ai(?:'s)?)\s+(?:creator|maker|developer|founder|builder|author|owner)\b",
    r"\bwho\s+(?:created|built|developed|founded|owns)\s+(?:the\s+)?beast\s*ai\b",
    r"\bwho\s+(?:is|are)\s+behind\s+(?:you|beast(?:\s*ai)?)\b",
    r"\b(?:were|are)\s+you\s+(?:made|created|built|developed)\s+by\s+(?:whom|who)\b",
    r"\bdid\s+chiranth\s+(?:make|create|build|develop)\s+(?:you|beast(?:\s*ai)?)\b",
    r"\bwho\s+is\s+the\s+creator\s+of\s+(?:you|beast(?:\s*ai)?)\b",
))


def _is_creator_question(message: str) -> bool:
    """Recognize common ways users ask who created or built Beast AI."""
    normalized = re.sub(r"\s+", " ", message).strip()
    return any(pattern.search(normalized) for pattern in _CREATOR_QUESTION_PATTERNS)


def _ensure_reply_emoji(text: str) -> str:
    """Guarantee at least one emoji in a user-facing text reply."""
    cleaned = text.rstrip()
    if not cleaned:
        return "🙂"
    if _EMOJI_PATTERN.search(cleaned):
        return cleaned
    return f"{cleaned} 🙂"


@app.after_request
def _enforce_emoji_on_text_replies(response):
    """Safety net for generated answers and text errors; preserve media URLs."""
    if not response.is_json:
        return response
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        return response
    reply = payload.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return response
    # The frontend detects a raw URL to render generated images/videos.
    if re.fullmatch(r"https?://\S+", reply.strip()):
        return response
    updated = _ensure_reply_emoji(reply)
    if updated != reply:
        payload["reply"] = updated
        response.set_data(app.json.dumps(payload))
        response.headers["Content-Type"] = "application/json"
    return response


def configured_gemini_keys() -> list[str]:
    return [
        value.strip()
        for name in ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(1, 10)]
        if (value := os.environ.get(name)) and value.strip()
    ]



def _chat_history(raw: Any) -> list[dict[str, str]]:
    """Parse frontend chat history into normalized provider messages.

    The current frontend sends [{type: "user"|"beast", message: "..."}],
    while some clients send [{role: "user"|"assistant", text: "..."}].
    Accept both formats and ignore malformed entries safely.
    """
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    else:
        items = raw

    if not isinstance(items, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in items[-100:]:
        if not isinstance(item, dict):
            continue

        raw_role = item.get("role") or item.get("type") or "user"
        role_value = str(raw_role).strip().lower()
        if role_value in {"user", "human"}:
            role = "user"
        elif role_value in {"assistant", "model", "beast", "beast ai", "ai"}:
            role = "assistant"
        else:
            continue

        raw_text = item.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raw_text = item.get("message")
        if not isinstance(raw_text, str):
            raw_text = item.get("content")
        if not isinstance(raw_text, str):
            continue

        text = raw_text.strip()
        if not text:
            continue
        normalized.append({"role": role, "text": text[:16000]})

    # Keep a bounded recent window so repeated chats cannot grow request payloads forever.
    return normalized[-40:]

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
    for key in ("url", "image_url", "video_url", "videoUrl", "output_url", "download_url", "uri"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip().startswith(("https://", "http://")):
            return value.strip()
    for key in ("data", "image", "images", "video", "videos", "output", "outputs", "result", "results", "resultUrls", "result_urls", "artifacts"):
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


def _cloudflare_image_bytes(prompt: str) -> tuple[bytes, str]:
    """Generate an image with Cloudflare Workers AI and return (bytes, MIME type)."""
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise RuntimeError(
            "Image generation is not configured. Add CLOUDFLARE_ACCOUNT_ID and "
            "CLOUDFLARE_API_TOKEN to your hosting environment, then redeploy. 🔑"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", CLOUDFLARE_ACCOUNT_ID):
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID does not look valid. Check the Cloudflare dashboard. ⚠️")
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ValueError("Describe the image you want to generate. 🎨")
    if len(clean_prompt) > 2048:
        clean_prompt = clean_prompt[:2048]

    model_path = urllib.parse.quote(CLOUDFLARE_IMAGE_MODEL, safe="@/")
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{model_path}"
    # FLUX.1 Schnell REST schema rejects `seed` in this account/API schema.
    # Keep only documented request fields.
    payload = {"prompt": clean_prompt}
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "image/jpeg, application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45 if IS_VERCEL else 75) as response:
        raw = response.read()
        content_type = response.headers.get_content_type().lower()

    if content_type.startswith("image/"):
        return raw, content_type

    # The Workers AI REST API commonly wraps its base64 image in result.image.
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Some compatible response paths may return raw image bytes with a generic MIME type.
        if raw.startswith(b"\xff\xd8\xff"):
            return raw, "image/jpeg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return raw, "image/png"
        raise RuntimeError(f"Cloudflare Workers AI returned an unrecognized response (Content-Type: {content_type}).")

    if not isinstance(data, dict):
        raise RuntimeError("Cloudflare Workers AI returned an unexpected response.")
    if data.get("success") is False:
        errors = data.get("errors") or []
        detail = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
        raise RuntimeError("Cloudflare Workers AI rejected the request: " + (detail[:700] or "unknown API error"))
    result = data.get("result", data)
    encoded = result.get("image") if isinstance(result, dict) else None
    if not isinstance(encoded, str) or not encoded.strip():
        raise RuntimeError("Cloudflare Workers AI response did not include an image.")
    encoded = encoded.strip()
    mime_type = "image/jpeg"
    if encoded.startswith("data:") and "," in encoded:
        data_header, encoded = encoded.split(",", 1)
        if ";" in data_header:
            mime_type = data_header[5:].split(";", 1)[0] or mime_type
    try:
        image_bytes = base64.b64decode(encoded, validate=False)
    except Exception as exc:
        raise RuntimeError("Cloudflare Workers AI returned invalid base64 image data.") from exc
    if not image_bytes:
        raise RuntimeError("Cloudflare Workers AI returned an empty image.")
    return image_bytes, mime_type if mime_type.startswith("image/") else "image/jpeg"


def _image_error_svg(message: str) -> bytes:
    """Show a readable error inside the generated-image slot if the provider fails."""
    safe = html.escape(message[:220])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="190" viewBox="0 0 720 190">'
        '<rect width="100%" height="100%" rx="16" fill="#f7f7f7" stroke="#d4d4d4"/>'
        '<text x="24" y="48" font-family="Arial,sans-serif" font-size="22" font-weight="700" fill="#222">Image generation failed ⚠️</text>'
        f'<foreignObject x="24" y="65" width="672" height="105"><div xmlns="http://www.w3.org/1999/xhtml" '
        'style="font:15px Arial,sans-serif;color:#444;overflow-wrap:anywhere;">'
        f'{safe}</div></foreignObject></svg>'
    )
    return svg.encode("utf-8")


def _start_cloudflare_image(message: str):
    """Return Markdown pointing at our backend image proxy; no frontend changes needed."""
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        return jsonify({
            "reply": "Image generation needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in your hosting environment. Add both and redeploy. 🔑",
            "error": True,
        }), 200
    # The browser requests this same-origin endpoint to receive the actual image bytes.
    image_base = os.environ.get("PUBLIC_API_BASE_URL", "https://beast-ai-v2.onrender.com").rstrip("/")
    image_url = image_base + "/api/media/image?" + urllib.parse.urlencode({"prompt": message[:2048]})
    return jsonify({
        "reply": f"![Generated image](<{image_url}>)\n\n🎨",
        "provider": "Cloudflare Workers AI · FLUX.1 Schnell",
    }), 200


@app.route("/api/media/image", methods=["GET"])
def cloudflare_media_image():
    """Proxy one generated image as bytes so the current frontend can render it without JS changes."""
    prompt = request.args.get("prompt", "").strip()
    if not prompt:
        return app.response_class(_image_error_svg("The image prompt is missing."), status=400, mimetype="image/svg+xml")
    try:
        image_bytes, mime_type = _cloudflare_image_bytes(prompt)
        response = app.response_class(image_bytes, status=200, mimetype=mime_type)
        # Browser caching avoids regenerating the exact same prompt when the image is revisited.
        response.headers["Cache-Control"] = "public, max-age=1800"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except Exception as exc:
        app.logger.exception("Cloudflare Workers AI image generation failed")
        message = _provider_error(exc, "Cloudflare Workers AI") if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError)) else str(exc)
        return app.response_class(_image_error_svg(message), status=200, mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


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


def _generate_with_groq(message: str, history: list[dict[str, str]], system_prompt: str) -> tuple[str | None, list[str]]:
    """Fast text-only fallback using the same Groq key as Whisper, when configured."""
    if not GROQ_API_KEY:
        return None, []
    try:
        text = _openai_compatible_chat(
            "https://api.groq.com/openai/v1/chat/completions",
            GROQ_API_KEY, GROQ_CHAT_MODEL, history, message, system_prompt,
            timeout=8,
        )
        return text, []
    except Exception as exc:
        return None, [_provider_error(exc, "Groq chat")]


def _generate_with_gemini(keys: list[str], message: str, history: list[dict[str, str]], files: list[dict[str, Any]], system_prompt: str, started: float) -> tuple[str | None, list[str]]:
    """Try the configured Gemini model and current stable fallbacks with hard time limits."""
    errors: list[str] = []
    if not GENAI_AVAILABLE:
        return None, ["Python dependency google-genai is not installed. Re-deploy after installing requirements.txt."]
    if not keys:
        return None, ["No Gemini API key is configured. Add GEMINI_API_KEY (or GEMINI_API_KEY_1 through _9)."]

    # Respect a custom GEMINI_MODEL first, but do not let a bad/overloaded model
    # block normal chat. These IDs are confirmed in Google's current model guide.
    model_candidates: list[str] = []
    for candidate in (GEMINI_MODEL, "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.7-flash"):
        candidate = str(candidate or "").strip()
        if candidate and candidate not in model_candidates:
            model_candidates.append(candidate)

    keys_to_try = list(keys)
    random.shuffle(keys_to_try)

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

    # Prefer trying several models with one key before burning the entire request
    # window cycling through keys for the same model. Every SDK call is single-shot;
    # fallback/retry decisions are made here under our own time budget.
    attempts = [(key, model) for model in model_candidates for key in keys_to_try]
    timeout_seconds = GEMINI_TIMEOUT_MS / 1000
    for key, model in attempts:
        if time.time() - started >= GEMINI_BUDGET_SECONDS - timeout_seconds:
            errors.append("Gemini fallback budget reached; stopped further attempts so the chat request can finish.")
            break
        try:
            client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(
                    timeout=GEMINI_TIMEOUT_MS,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    automatic_continuation=False,
                ),
            )
            result_text = response.text
            if isinstance(result_text, str) and result_text.strip():
                return result_text.strip(), errors
            errors.append(f"Gemini model {model} returned no text.")
        except Exception as exc:
            description = str(exc)
            if "safety" in description.lower():
                return "The request was blocked by Gemini's safety filters. ⚠️", errors
            # Keep UI/error responses short; detailed provider error is still in server logs.
            app.logger.warning("Gemini request failed for model %s: %s", model, description[:500])
            errors.append(f"Gemini model {model}: {description[:350]}")
    return None, errors


@app.route("/api/live/transcribe", methods=["POST"])
def groq_live_transcribe():
    """Transcribe one recorded voice turn with Groq Whisper. API key stays server-side."""
    if not GROQ_API_KEY:
        return jsonify({"error": "Voice mode is not configured. Add GROQ_API_KEY to your hosting environment and redeploy. 🎙️"}), 503
    upload = request.files.get("audio") or request.files.get("file")
    if upload is None:
        return jsonify({"error": "No audio file received. Allow microphone access and try again. 🎙️"}), 400
    audio_bytes = upload.read()
    if not audio_bytes:
        return jsonify({"error": "The audio recording was empty. Please record again. 🎙️"}), 400
    # Vercel requests have a small body limit; fail with a useful message before contacting Groq.
    if len(audio_bytes) > (3_700_000 if IS_VERCEL else 18_000_000):
        return jsonify({"error": "That voice recording is too large. Keep each voice turn under about 45 seconds and try again. 🎙️"}), 413
    mime_type = (upload.mimetype or "audio/webm").split(";")[0].strip().lower()
    extensions = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac"}
    extension = extensions.get(mime_type, "webm")
    filename = f"beast-voice.{extension}"
    boundary = "----BeastAIVoice" + str(random.randint(10**10, 10**12 - 1))
    crlf = b"\r\n"
    parts = []
    def field(name: str, value: str) -> None:
        parts.extend([b"--" + boundary.encode(), crlf,
                      f'Content-Disposition: form-data; name="{name}"'.encode(), crlf,
                      b"Content-Type: text/plain; charset=utf-8", crlf, crlf,
                      value.encode("utf-8"), crlf])
    field("model", GROQ_WHISPER_MODEL)
    field("response_format", "json")
    field("temperature", "0")
    parts.extend([b"--" + boundary.encode(), crlf,
                  f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(), crlf,
                  f"Content-Type: {mime_type}".encode(), crlf, crlf,
                  audio_bytes, crlf,
                  b"--" + boundary.encode() + b"--", crlf])
    body = b"".join(parts)
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40 if IS_VERCEL else 60) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
        transcript = str(result.get("text", "")).strip() if isinstance(result, dict) else ""
        if not transcript:
            return jsonify({"error": "Groq Whisper returned an empty transcript. Try speaking more clearly. 🎙️"}), 502
        return jsonify({"text": transcript, "model": GROQ_WHISPER_MODEL}), 200
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:700]
        app.logger.warning("Groq Whisper returned HTTP %s: %s", exc.code, detail)
        return jsonify({"error": f"Groq Whisper returned HTTP {exc.code}: {detail or 'No details provided.'}"}), exc.code
    except (urllib.error.URLError, TimeoutError) as exc:
        app.logger.warning("Groq Whisper connection failed: %s", exc)
        return jsonify({"error": "Could not reach Groq Whisper. Check connectivity and try again. 🎙️"}), 502
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        app.logger.warning("Groq Whisper returned invalid JSON: %s", exc)
        return jsonify({"error": "Groq Whisper returned an unreadable response. Please retry. 🎙️"}), 502


@app.route("/api/live/token", methods=["POST"])
def create_gemini_live_token():
    """Create a short-lived, single-use Gemini Live token for the browser."""
    if not GENAI_AVAILABLE or genai is None:
        return jsonify({"error": "The google-genai package is not installed. Check deployment build logs."}), 503
    keys = configured_gemini_keys()
    if not keys:
        return jsonify({"error": "Gemini Live needs GEMINI_API_KEY (or GEMINI_API_KEY_1 through _9) configured on the server."}), 503
    try:
        now = datetime.now(timezone.utc)
        client = genai.Client(api_key=keys[0])
        token = client.auth_tokens.create(config={
            "uses": 1,
            "expire_time": now + timedelta(minutes=30),
            "new_session_expire_time": now + timedelta(minutes=1),
            "live_connect_constraints": {
                "model": GEMINI_LIVE_MODEL,
                "config": {
                    "response_modalities": ["AUDIO"],
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                    "system_instruction": {"parts": [{"text": LIVE_SYSTEM_INSTRUCTION}]},
                },
            },
        })
        token_value = getattr(token, "name", None)
        if not token_value:
            raise RuntimeError("Gemini did not return an ephemeral token.")
        return jsonify({"token": token_value, "model": GEMINI_LIVE_MODEL}), 200
    except Exception as exc:
        app.logger.exception("Gemini Live token creation failed")
        detail = str(exc).strip() or exc.__class__.__name__
        return jsonify({"error": "Could not create a Gemini Live session token: " + detail[:900]}), 502


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
            "gemini_live": "Groq Whisper transcription + browser speech output",
            "groq_whisper": GROQ_WHISPER_MODEL,
            "groq_chat": GROQ_CHAT_MODEL,
            "grok": GROK_MODEL,
            "kira_fast": KIRA_FLASH_MODEL,
            "kira_pro": KIRA_PRO_MODEL,
            "cloudflare_image": CLOUDFLARE_IMAGE_MODEL,
            "veo_3_1_quality": APIPASS_VEO_QUALITY_MODEL,
            "veo_3_1_fast": APIPASS_VEO_FAST_MODEL,
            "video_resolution": APIPASS_VIDEO_RESOLUTION,
            "video_duration_seconds": int(APIPASS_VIDEO_DURATION),
        },
        "features": {
            "chat": True,
            "live_voice": bool(GROQ_API_KEY),
            "groq_whisper": bool(GROQ_API_KEY),
            "groq_chat": bool(GROQ_API_KEY),
            "attachments": GENAI_AVAILABLE and bool(keys),
            "image_generation": bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN),
            "video_generation": bool(APIPASS_API_KEY),
            "video_generation_fast": bool(APIPASS_API_KEY),
            "apipass_configured": bool(APIPASS_API_KEY),
            "kira_pro_chat": bool(os.environ.get("KIRA_PRO_API_KEY")),
        },
    }), 200


# Compatibility: some old clients pinged /api/chat with GET. Return health instead of 405.
@app.route("/api/chat", methods=["GET"])
def chat_health_compat():
    return health_check()


@app.route("/api/media/status", methods=["POST"])
def media_status():
    body = _json_request_body()
    mode = body.get("mode", "image")
    request_id = body.get("request_id", "")
    provider = str(body.get("provider", "")).strip().lower()
    if provider in {"apipass", "api pass"}:
        return _poll_apipass_video(request_id)
    return jsonify({"reply": "Unknown media provider. Start a new generation with the current Beast AI version. ⚠️", "error": True}), 400


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

        # Answer creator questions deterministically rather than relying on a provider prompt.
        if mode == "chat" and not uploaded_files and message and _is_creator_question(message):
            return jsonify({"reply": CREATOR_REPLY, "provider": "Beast AI"}), 200

        if mode == "image":
            if not message:
                return jsonify({"reply": "Describe the image you want to generate. 🎨"}), 200
            return _start_cloudflare_image(message)
        if mode in {"video", "video-fast"}:
            if not message:
                return jsonify({"reply": "Describe the video clip you want to create. 🎬"}), 200
            # Use API Pass only for native Google Veo 3.1 video generation.
            return _start_apipass_video(message, mode)

        ist = timezone(timedelta(hours=5, minutes=30))
        live_time = datetime.now(ist).strftime("%A, %d %B %Y, %I:%M %p IST")
        system_prompt = (
            "You are Beast AI, a friendly, useful, witty assistant. Be clear and direct.\n"
            "Creator identity: Beast AI was created by Chiranth when he was in 8th grade. "
            "If asked who created, made, built, developed, founded, or owns Beast AI, answer that clearly. "
            "Be transparent that the underlying AI models are provided by external services; do not claim Chiranth personally trained those models.\n"
            "Emoji rule: Every user-facing text reply must include at least one relevant emoji. "
            "If your draft has none, add one naturally at the end. Do not overuse emojis.\n"
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
                        history, message, system_prompt, timeout=8,
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
                        history, message, system_prompt, timeout=8,
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
                        history, message, system_prompt, timeout=8,
                    )
                    used_provider = "Kira Fast"
                except Exception as exc:
                    errors.append(_provider_error(exc, "Kira Fast"))
            else:
                errors.append("KIRA_FLASH_API_KEY is not configured; falling back to Gemini.")

        # Normal text chat uses Groq first when its key is present. This avoids making
        # every ordinary message wait on a temporarily overloaded Gemini endpoint.
        # Attachments still go to Gemini because this text endpoint cannot inspect files.
        if not final_text and not uploaded_files and speed == "normal":
            final_text, groq_errors = _generate_with_groq(message, history, system_prompt)
            errors.extend(groq_errors)
            if final_text:
                used_provider = "Groq"

        if not final_text:
            final_text, gemini_errors = _generate_with_gemini(
                gemini_keys, message, history, uploaded_files, system_prompt, started,
            )
            errors.extend(gemini_errors)
            if final_text:
                used_provider = "Gemini"

        # Groq is also the next fallback for selected tiers or when Gemini is overloaded.
        if not final_text and not uploaded_files:
            final_text, groq_errors = _generate_with_groq(message, history, system_prompt)
            errors.extend(groq_errors)
            if final_text:
                used_provider = "Groq fallback"

        # Last-resort text fallback is used only for text-only requests. It cannot inspect attachments.
        if not final_text and not uploaded_files:
            try:
                encoded_prompt = urllib.parse.quote(message, safe="")
                encoded_system = urllib.parse.quote(system_prompt, safe="")
                fallback_url = f"https://text.pollinations.ai/{encoded_prompt}?system={encoded_system}"
                req = urllib.request.Request(fallback_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=5) as response:
                    final_text = response.read().decode("utf-8", errors="replace").strip()
                    used_provider = "Pollinations fallback"
            except Exception as exc:
                errors.append(_provider_error(exc, "Text fallback"))

        if not final_text:
            app.logger.error("All text providers failed for this request: %s", " | ".join(errors[-8:]))
            return jsonify({
                "reply": "I couldn't get a response from the text models just now. Please try again in a moment. If this keeps happening, check the hosting logs and the configured API keys. ⚠️",
                "error": True,
            }), 503

        notice = ""
        if used_provider == "Pollinations fallback":
            notice = "My primary text model was temporarily unavailable, so I used a backup provider. 🔄"
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
