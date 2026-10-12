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
GEMINI_TIMEOUT_MS = 12_000 if IS_VERCEL else 25_000
GEMINI_BUDGET_SECONDS = 40 if IS_VERCEL else 90
VIDEO_TIMEOUT_SECONDS = 45 if IS_VERCEL else 75
KIRA_API_BASE_URL = os.environ.get("KIRA_API_BASE_URL", "https://kiraai.vn/api/v1").rstrip("/")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4.6").strip()
KIRA_FLASH_MODEL = os.environ.get("KIRA_FLASH_MODEL", "kira-3.5-flash").strip()
KIRA_PRO_MODEL = os.environ.get("KIRA_PRO_MODEL", "kira-3.5-pro").strip()

# Images use Cloudflare Workers AI (FLUX.1 Schnell) so image generation can
# use Cloudflare's free daily allocation instead of fal.ai credits.
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
CLOUDFLARE_IMAGE_MODEL = os.environ.get(
    "CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"
).strip()

# fal.ai is retained for optional video generation only. Leave ENABLE_FAL_VIDEO
# unset for the free image-only configuration; set it to true only if video
# billing/credits are available in your fal.ai account.
FAL_KEY = (os.environ.get("FAL_API_KEY", "").strip() or os.environ.get("FAL_KEY", "").strip())
FAL_IMAGE_MODEL = os.environ.get("FAL_IMAGE_MODEL", "fal-ai/nano-banana-2").strip()
FAL_VIDEO_MODEL = os.environ.get("FAL_VIDEO_MODEL", "fal-ai/kling-video/v3/standard/text-to-video").strip()
FAL_VIDEO_FAST_MODEL = os.environ.get("FAL_VIDEO_FAST_MODEL", "fal-ai/ltx-2.3/text-to-video/fast").strip()
ENABLE_FAL_VIDEO = os.environ.get("ENABLE_FAL_VIDEO", "false").strip().lower() in {"1", "true", "yes", "on"}

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
    for key in ("url", "image_url", "video_url", "output_url", "download_url", "uri"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip().startswith(("https://", "http://")):
            return value.strip()
    for key in ("data", "image", "images", "video", "videos", "output", "outputs", "result", "results", "artifacts"):
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
    payload = {
        "prompt": clean_prompt,
        "steps": 4,
        "seed": random.randint(1, 2_147_483_647),
    }
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
    image_url = "/api/media/image?" + urllib.parse.urlencode({"prompt": message[:2048]})
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


def _fal_http_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    """Call fal.ai's authenticated queue API from the server, never from browser code."""
    if not FAL_KEY:
        raise RuntimeError("FAL_API_KEY (or legacy FAL_KEY) is not configured in the hosting environment.")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Key {FAL_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"fal.ai returned non-JSON response (HTTP {response.status}).") from exc
        if not isinstance(data, dict):
            raise ValueError("fal.ai returned an unexpected JSON response.")
        return data


def _fal_config_for_mode(mode: str) -> tuple[str, str, dict[str, Any]]:
    """Return model endpoint, media type, and provider-specific input payload."""
    prompt = ""  # replaced by caller after selecting the endpoint
    if mode == "image":
        endpoint = FAL_IMAGE_MODEL
        media_type = "image"
        payload = {"prompt": prompt, "num_images": 1, "aspect_ratio": "auto", "resolution": "1K", "output_format": "png"}
    elif mode == "video-fast":
        endpoint = FAL_VIDEO_FAST_MODEL
        media_type = "video"
        payload = {"prompt": prompt, "duration": 6, "resolution": "1080p", "aspect_ratio": "16:9", "fps": 25, "generate_audio": False}
    else:
        endpoint = FAL_VIDEO_MODEL
        media_type = "video"
        payload = {"prompt": prompt, "duration": "5", "generate_audio": False}
    # These values come from deployment environment variables, but still validate them
    # before using them in a URL path.
    if not endpoint or not re.fullmatch(r"[A-Za-z0-9._/-]+", endpoint) or ".." in endpoint:
        raise ValueError("Invalid fal.ai model endpoint configuration.")
    return endpoint, media_type, payload


def _start_fal_media_job(message: str, mode: str):
    if not FAL_KEY:
        return jsonify({
            "reply": "Image and video generation need a fal.ai API key. Add FAL_API_KEY in the hosting environment, then redeploy. 🔑",
            "error": True,
        }), 200
    try:
        endpoint, media_type, payload = _fal_config_for_mode(mode)
        payload["prompt"] = message
        submit_url = "https://queue.fal.run/" + urllib.parse.quote(endpoint, safe="/")
        submitted = _fal_http_json(submit_url, method="POST", payload=payload, timeout=20)
        request_id = submitted.get("request_id") or submitted.get("requestId")
        if not isinstance(request_id, str) or not request_id.strip():
            return jsonify({
                "reply": "fal.ai accepted no trackable job ID. Check the fal.ai API response and account access. ⚠️",
                "error": True,
            }), 200
        return jsonify({
            "media_pending": True,
            "request_id": request_id,
            "mode": mode,
            "media_type": media_type,
            "provider": "fal.ai",
        }), 202
    except Exception as exc:
        return jsonify({"reply": _provider_error(exc, "fal.ai media API"), "error": True}), 200


def _poll_fal_media_job(mode: str, request_id: str):
    if not FAL_KEY:
        return jsonify({"reply": "FAL_API_KEY is missing from the hosting environment. 🔑", "error": True}), 200
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", request_id):
        return jsonify({"reply": "The media job ID is invalid. Please start a new generation. ⚠️", "error": True}), 400
    if mode not in {"image", "video", "video-fast"}:
        return jsonify({"reply": "Unknown media generation mode. ⚠️", "error": True}), 400
    try:
        endpoint, media_type, _ = _fal_config_for_mode(mode)
        quoted_endpoint = urllib.parse.quote(endpoint, safe="/")
        quoted_id = urllib.parse.quote(request_id, safe="")
        status_url = f"https://queue.fal.run/{quoted_endpoint}/requests/{quoted_id}/status"
        status_payload = _fal_http_json(status_url, method="GET", timeout=15)
        status = str(status_payload.get("status", "")).upper()
        if status == "COMPLETED":
            result_url = f"https://queue.fal.run/{quoted_endpoint}/requests/{quoted_id}"
            result = _fal_http_json(result_url, method="GET", timeout=20)
            media_url = _find_media_url(result)
            if not media_url:
                return jsonify({"reply": "fal.ai marked the job complete but did not return a media URL. Check the provider result schema. ⚠️", "error": True}), 200
            return _media_response(media_type, media_url, "fal.ai")
        if status in {"FAILED", "ERROR", "CANCELLED"}:
            detail = status_payload.get("error") or status_payload.get("detail") or "The provider failed to generate this media."
            return jsonify({"reply": f"fal.ai generation failed ({status}): {str(detail)[:900]} ⚠️", "error": True}), 200
        return jsonify({"media_pending": True, "queue_status": status or "IN_QUEUE"}), 200
    except Exception as exc:
        return jsonify({"reply": _provider_error(exc, "fal.ai media status"), "error": True}), 200

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
            "cloudflare_image": CLOUDFLARE_IMAGE_MODEL,
            "fal_video": FAL_VIDEO_MODEL,
            "fal_video_fast": FAL_VIDEO_FAST_MODEL,
        },
        "features": {
            "chat": True,
            "attachments": GENAI_AVAILABLE and bool(keys),
            "image_generation": bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN),
            "video_generation": bool(FAL_KEY and ENABLE_FAL_VIDEO),
            "video_generation_fast": bool(FAL_KEY and ENABLE_FAL_VIDEO),
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
    return _poll_fal_media_job(mode, request_id)


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
                return jsonify({"reply": "Describe the video you want to generate. 🎬"}), 200
            if ENABLE_FAL_VIDEO and FAL_KEY:
                return _start_fal_media_job(message, mode)
            return jsonify({
                "reply": "Free Cloudflare Workers AI image generation is enabled, but this model does not generate videos. Video generation needs a separate video provider with available credits or a self-hosted video model. No paid video request was sent. 🎬",
                "error": True,
            }), 200

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
