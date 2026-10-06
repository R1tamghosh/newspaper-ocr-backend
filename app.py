"""
Newspaper OCR ML backend for Label Studio.

Implements the HTTP endpoints Label Studio expects from an ML backend:
  GET  /health   -> tells Label Studio the backend is alive
  POST /setup    -> called when you connect the backend to a project
  POST /predict  -> interactive mode (dormant, never worked reliably)
  POST /webhook  -> the real logic: annotation saved -> OCR -> PATCH back

Multi-user additions:
  GET/POST /settings -> each intern registers their own OCR key, verified
                        against their own Label Studio token
  /webhook           -> looks up the annotator's key by `completed_by`
                        in user_keys.json and uses it for that request only

No label_studio_ml SDK, no Redis, no queue - just Flask, Pillow and the
OCR provider SDKs.
"""

import fcntl
import io
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()  # reads .env in the current directory into os.environ

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("newspaper-ocr")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
SARVAM_LANGUAGE = os.environ.get("SARVAM_LANGUAGE", "bn-IN")

# Owner's personal keys from .env. Since the multi-user rework these are ONLY
# used by the dormant /predict route. The webhook never touches them - it
# uses the annotator's own key from user_keys.json (or skips OCR entirely).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")
OCR_PROVIDER = os.environ.get("OCR_PROVIDER", "gemini").lower()

PROVIDERS = ("gemini", "sarvam")

# Label Studio connection. LABEL_STUDIO_API_KEY is the *owner's* token, used
# by the backend itself to read tasks/annotations and PATCH results back.
LABEL_STUDIO_URL = os.environ.get("LABEL_STUDIO_URL", "http://localhost:8080")
LABEL_STUDIO_API_KEY = os.environ.get("LABEL_STUDIO_API_KEY", "")

# {label_studio_user_id (as string): {"provider": ..., "api_key": ...}}
USER_KEYS_PATH = os.environ.get(
    "USER_KEYS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_keys.json"),
)

# Tried in order; first one that returns JSON with an integer "id" wins.
# (Label Studio's docs badge "Get current user info" as Enterprise-only, so
# don't assume - verify against the running version.)
WHOAMI_PATHS = ("/api/current-user/whoami", "/api/current-user/")

PROMPT = (
    "Transcribe every word of text visible in this newspaper clipping, "
    "exactly as printed, preserving line breaks. Output only the "
    "transcribed text - no commentary, no quotes, no markdown."
)


# ---------------------------------------------------------------------------
# Label Studio helpers
# ---------------------------------------------------------------------------

class LSAuthError(Exception):
    """Raised when a Label Studio token can't be verified. The message is
    safe to show to the person filling in the /settings form."""


def ls_headers_for(token: str) -> dict:
    """Auth header for an arbitrary Label Studio token.

    Personal Access Tokens (PATs) are JWT refresh tokens - they can't be
    used directly. Exchange for a short-lived access token first, then use
    it as a Bearer token. Legacy tokens use "Token <key>" directly."""
    if not token:
        return {}
    if token.count(".") == 2:  # looks like a JWT -> PAT
        return {"Authorization": f"Bearer {get_ls_access_token(token)}"}
    return {"Authorization": f"Token {token}"}


def ls_headers() -> dict:
    """Headers for the backend's own calls (the owner's token from .env)."""
    return ls_headers_for(LABEL_STUDIO_API_KEY)


def get_ls_access_token(refresh_token: str) -> str:
    """Exchange a PAT (refresh token) for a short-lived access token.
    Called fresh every time rather than cached, since volume here is low
    and access tokens expire quickly."""
    url = f"{LABEL_STUDIO_URL.rstrip('/')}/api/token/refresh"
    resp = requests.post(url, json={"refresh": refresh_token}, timeout=15)
    resp.raise_for_status()
    return resp.json()["access"]


def verify_ls_token(token: str) -> dict:
    """Ask Label Studio who owns this token. Returns the user dict (has at
    least an integer "id"). Raises LSAuthError with a user-safe message.

    This is the identity check for one-time key registration: only someone
    who can produce a working token for an account can register a key
    against that account's user ID."""
    base = LABEL_STUDIO_URL.rstrip("/")

    try:
        headers = ls_headers_for(token)
    except requests.HTTPError:
        raise LSAuthError(
            "Label Studio rejected that token. Check you copied the whole "
            "token from Account & Settings."
        )
    except requests.RequestException:
        raise LSAuthError("Could not reach Label Studio to verify the token.")

    for path in WHOAMI_PATHS:
        try:
            resp = requests.get(base + path, headers=headers, timeout=15)
        except requests.RequestException:
            raise LSAuthError("Could not reach Label Studio to verify the token.")

        if resp.status_code in (401, 403):
            raise LSAuthError(
                "Label Studio rejected that token. Check you copied the whole "
                "token from Account & Settings."
            )
        if not resp.ok:
            continue  # e.g. 404 -> this path doesn't exist on this version
        try:
            body = resp.json()
        except ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("id"), int):
            return body

    raise LSAuthError(
        "Could not verify your identity with Label Studio (no user endpoint "
        "responded as expected). Tell the project owner."
    )


def load_task_image(image_ref: str) -> Image.Image:
    """Download the task image, whether it's a full URL or a Label Studio
    local-storage path like /data/upload/1/xyz.jpg."""
    if image_ref.startswith("http://") or image_ref.startswith("https://"):
        url = image_ref
    else:
        url = LABEL_STUDIO_URL.rstrip("/") + image_ref

    resp = requests.get(url, headers=ls_headers(), timeout=30)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def crop_from_percent_box(img: Image.Image, region_value: dict) -> Image.Image:
    """Label Studio gives rectangle coordinates as % of image width/height,
    plus the true pixel size of the original image. Convert to a pixel crop,
    with a small padding margin since OCR quality suffers from tight crops."""
    w, h = img.size
    x = region_value["x"] / 100.0 * w
    y = region_value["y"] / 100.0 * h
    box_w = region_value["width"] / 100.0 * w
    box_h = region_value["height"] / 100.0 * h

    pad = 4
    left = max(0, x - pad)
    top = max(0, y - pad)
    right = min(w, x + box_w + pad)
    bottom = min(h, y + box_h + pad)
    return img.crop((left, top, right, bottom))


def fetch_annotation(annotation_id) -> dict:
    url = f"{LABEL_STUDIO_URL.rstrip('/')}/api/annotations/{annotation_id}/"
    resp = requests.get(url, headers=ls_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_task(task_id) -> dict:
    url = f"{LABEL_STUDIO_URL.rstrip('/')}/api/tasks/{task_id}/"
    resp = requests.get(url, headers=ls_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def patch_annotation(annotation_id, result: list) -> dict:
    url = f"{LABEL_STUDIO_URL.rstrip('/')}/api/annotations/{annotation_id}/"
    resp = requests.patch(
        url, headers=ls_headers(), json={"result": result}, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Per-user key storage (user_keys.json)
# ---------------------------------------------------------------------------

@contextmanager
def _keys_file_lock():
    """Cross-thread AND cross-process lock (flock on a sidecar file), so two
    interns registering at the same moment can't lose each other's update,
    even if the app later runs under several gunicorn workers."""
    with open(USER_KEYS_PATH + ".lock", "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _read_keys_file() -> dict:
    try:
        with open(USER_KEYS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        log.error("%s is not valid JSON - treating as empty", USER_KEYS_PATH)
        return {}


def get_user_creds(user_id) -> dict | None:
    """Re-reads the file every call (it's tiny), so a newly registered
    intern works immediately with no restart. Writes are atomic
    (os.replace), so a read never sees a half-written file."""
    if user_id is None:
        return None
    entry = _read_keys_file().get(str(user_id))
    if entry and entry.get("provider") in PROVIDERS and entry.get("api_key"):
        return entry
    return None


def save_user_creds(user_id: int, provider: str, api_key: str, username=None):
    """Create or update one user's entry. Atomic write, file mode 600."""
    with _keys_file_lock():
        data = _read_keys_file()
        data[str(user_id)] = {
            "provider": provider,
            "api_key": api_key,
            "username": username,
        }
        directory = os.path.dirname(USER_KEYS_PATH)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".user_keys_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, USER_KEYS_PATH)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise


# ---------------------------------------------------------------------------
# OCR providers - every call takes its own key; nothing here reads a shared
# mutable "current key", because /webhook runs one thread per request and
# several interns' requests are genuinely in flight at once.
# ---------------------------------------------------------------------------

def ocr_with_gemini(crop_img: Image.Image, api_key: str) -> str:
    client = genai.Client(api_key=api_key)  # built per call, this key only

    buf = io.BytesIO()
    crop_img.save(buf, format="PNG")

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            PROMPT,
            types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"),
        ],
    )
    return (response.text or "").strip()


def ocr_with_sarvam(crop_img: Image.Image, api_key: str) -> str:
    from sarvamai import SarvamAI

    client = SarvamAI(api_subscription_key=api_key)  # this key only

    buf = io.BytesIO()
    crop_img.save(buf, format="PNG")
    buf.seek(0)

    # Step 1: start the job. Note "digitise" (Sarvam's spelling), and the
    # file goes in as a list - a (filename, filelike) tuple works.
    start = client.doc_ai.digitise(
        file=[("crop.png", buf)],
        language=SARVAM_LANGUAGE,
        output_format="md",
    )
    job_id = start.job_id

    # Step 2: poll until the job finishes. digitise() only *starts* the job.
    for _ in range(30):  # ~30s max wait, adjust if needed
        status = client.doc_ai.get_status(job_id)
        if status.status in ("completed", "partially_completed"):
            break
        if status.status in ("failed", "rejected"):
            raise RuntimeError(f"Sarvam job {status.status}")
        time.sleep(1)
    else:
        raise RuntimeError("Sarvam job timed out")

    # Step 3: fetch the actual text once the job is done.
    results = client.doc_ai.get_results(job_id)

    pieces = []
    for document in results.documents or []:
        for page in document.pages or []:
            blocks = sorted(page.blocks or [], key=lambda b: b.get("reading_order", 0))
            for block in blocks:
                text = block.get("text")
                if text:
                    pieces.append(text)
    return "\n".join(pieces).strip()


def ocr_transcribe(crop_img: Image.Image, provider: str, api_key: str) -> str:
    """Single entry point - dispatches on the provider and uses exactly the
    key it was handed. Raises on failure (bad/exhausted key, job failed...)."""
    if not api_key:
        raise RuntimeError("No API key supplied")
    if provider == "sarvam":
        return ocr_with_sarvam(crop_img, api_key)
    if provider == "gemini":
        return ocr_with_gemini(crop_img, api_key)
    raise ValueError(f"Unknown OCR provider: {provider!r}")


SKIP_OCR_LABELS = {"Image"}  # regions with these labels are never OCR'd


def ocr_missing_regions(
    image_ref: str, result: list, provider: str, api_key: str
) -> tuple[list, bool]:
    """Given an annotation's `result` list (mix of rectanglelabels + textarea
    entries sharing an `id`), OCR any rectangle that doesn't already have
    non-empty transcribed text, and return the updated result list."""
    by_id: dict = {}
    for item in result:
        by_id.setdefault(item["id"], {})[item["type"]] = item

    img = None
    updated = list(result)
    index_by_key = {(item["id"], item["type"]): i for i, item in enumerate(result)}
    changed = False

    for region_id, items in by_id.items():
        rect = items.get("rectanglelabels")
        if not rect:
            continue

        labels = rect["value"].get("rectanglelabels") or []
        if any(l in SKIP_OCR_LABELS for l in labels):
            continue  # e.g. a photo/picture region - nothing to transcribe

        textarea = items.get("textarea")
        existing_text = ""
        if textarea:
            texts = textarea.get("value", {}).get("text") or []
            existing_text = texts[0] if texts else ""
        if existing_text.strip():
            continue  # already transcribed (or human-edited) - don't overwrite

        if img is None:
            img = load_task_image(image_ref)
        crop = crop_from_percent_box(img, rect["value"])

        # A bad/exhausted per-user key raises here. Leave the region blank so
        # the intern can type it manually (Bengali phonetic script).
        try:
            text = ocr_transcribe(crop, provider, api_key)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "OCR failed for region %s (%s): %s: %s - leaving untouched",
                region_id, provider, type(exc).__name__, exc,
            )
            continue

        if not text.strip() or text.startswith("[Sarvam job") or text.startswith("[OCR error"):
            log.warning("Empty/failed OCR result for region %s, leaving untouched", region_id)
            continue  # don't write empty text - avoids retry-loop on next webhook
        changed = True

        new_textarea = {
            "id": region_id,
            "type": "textarea",
            "from_name": "transcription",
            "to_name": rect.get("to_name", "page"),
            "original_width": rect.get("original_width"),
            "original_height": rect.get("original_height"),
            "value": {"text": [text]},
        }
        key = (region_id, "textarea")
        if key in index_by_key:
            updated[index_by_key[key]] = new_textarea
        else:
            updated.append(new_textarea)

    return updated, changed


# ---------------------------------------------------------------------------
# Network exposure guard
# ---------------------------------------------------------------------------
# The backend listens on all interfaces so interns can reach /settings from
# other machines. Everything else (/webhook, /predict, /setup) is meant only
# for Label Studio running on this same machine, so refuse it from anywhere
# else - otherwise anyone on the network could POST fake webhooks and burn a
# registered intern's OCR quota.

LOOPBACK_ADDRS = {"127.0.0.1", "::1"}
NETWORK_VISIBLE_PATHS = {"/", "/health", "/settings"}


@app.before_request
def restrict_internal_routes():
    if request.path in NETWORK_VISIBLE_PATHS:
        return None
    if request.remote_addr not in LOOPBACK_ADDRS:
        log.warning("Blocked %s %s from non-local address %s",
                    request.method, request.path, request.remote_addr)
        return jsonify({"error": "forbidden"}), 403
    return None


# ---------------------------------------------------------------------------
# Label Studio ML backend contract
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "UP", "model": GEMINI_MODEL})


@app.route("/setup", methods=["POST"])
def setup():
    # Nothing to initialize per-project; just acknowledge.
    return jsonify({"model_version": "gemini-ocr-1"})


@app.route("/predict", methods=["POST"])
def predict():
    data = request.json or {}
    log.info("=== /predict raw request ===\n%s", data)

    tasks = data.get("tasks") or []
    params = data.get("params") or {}
    context = params.get("context") or {}

    if not tasks:
        return jsonify({"results": []})

    task = tasks[0]

    context_results = context.get("result") or []
    log.info("=== context.result has %d region(s) ===", len(context_results))
    if not context_results:
        return jsonify({"results": [{"result": []}]})

    region = context_results[0]
    region_value = region["value"]

    image_ref = task["data"].get("image") or task["data"].get("img")
    if not image_ref:
        log.error("Task data has no 'image' key: %s", task["data"])
        return jsonify({"results": [{"result": []}]})

    try:
        img = load_task_image(image_ref)
        crop = crop_from_percent_box(img, region_value)
        # Dormant route: uses the owner's .env key, not per-user keys.
        owner_key = GEMINI_API_KEY if OCR_PROVIDER == "gemini" else SARVAM_API_KEY
        text = ocr_transcribe(crop, OCR_PROVIDER, owner_key)
    except Exception as exc:  # noqa: BLE001
        log.exception("OCR failed")
        text = f"[OCR error: {exc}]"

    prediction_result = [
        {
            "id": region["id"],
            "type": "textarea",
            "from_name": "transcription",
            "to_name": region.get("to_name", "page"),
            "original_width": region.get("original_width"),
            "original_height": region.get("original_height"),
            "value": {
                **region_value,
                "text": [text],
            },
        }
    ]

    response_body = {"results": [{"result": prediction_result}]}
    log.info("=== /predict response ===\n%s", response_body)
    return jsonify(response_body)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    action = data.get("action")
    log.info("=== /webhook action=%s payload keys=%s ===", action, list(data.keys()))

    if action not in ("ANNOTATION_CREATED", "ANNOTATION_UPDATED"):
        return jsonify({"status": "ignored", "action": action}), 200

    # Hand off the slow part (OCR + PATCH) to a background thread so we can
    # reply to Label Studio immediately. Sarvam's job-based OCR can take
    # 10-30+ seconds, far longer than Label Studio waits for a response.
    threading.Thread(target=process_annotation_webhook, args=(data,), daemon=True).start()
    return jsonify({"status": "accepted"}), 200


def process_annotation_webhook(data):
    """Runs off the request thread. Takes the already-parsed JSON dict, not
    the Flask request object (not usable outside the request context)."""
    annotation = data.get("annotation")
    task = data.get("task")

    if not annotation or "result" not in annotation:
        annotation_id = (annotation or {}).get("id") or data.get("annotation_id")
        if not annotation_id:
            log.error("No annotation id in webhook payload, can't proceed")
            return
        log.info("Fetching full annotation %s from API", annotation_id)
        annotation = fetch_annotation(annotation_id)

    # Who is this? completed_by comes server-to-server from Label Studio, so
    # it's trustworthy without any re-verification.
    user_id = annotation.get("completed_by") or data.get("completed_by")
    if isinstance(user_id, dict):  # some versions nest the user object
        user_id = user_id.get("id")

    creds = get_user_creds(user_id)
    if creds is None:
        # Not registered (or no ID at all): skip OCR rather than silently
        # spending the owner's quota. The region stays blank -> manual typing.
        log.warning(
            "No registered OCR key for Label Studio user %s - skipping OCR "
            "(they need to visit /settings)", user_id,
        )
        return

    provider, api_key = creds["provider"], creds["api_key"]
    # Last 4 chars only - enough to prove which key a request used.
    log.info("OCR for LS user %s via %s (key ...%s)", user_id, provider, api_key[-4:])

    if not task or "data" not in task:
        task_id = (task or {}).get("id") or annotation.get("task")
        if task_id:
            log.info("Fetching full task %s from API", task_id)
            task = fetch_task(task_id)

    if not task or "data" not in task:
        log.error("Could not resolve task data, giving up")
        return

    image_ref = task["data"].get("image") or task["data"].get("img")
    result = annotation.get("result") or []

    try:
        updated_result, changed = ocr_missing_regions(image_ref, result, provider, api_key)
    except Exception:
        log.exception("OCR failed while processing webhook")
        return

    if not changed:
        log.info("Nothing to OCR (all regions already transcribed)")
        return

    patch_annotation(annotation["id"], updated_result)
    log.info("=== patched annotation %s ===", annotation["id"])


# ---------------------------------------------------------------------------
# /settings - self-service OCR key registration
# ---------------------------------------------------------------------------

SETTINGS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OCR key settings</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 34rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
  label { display: block; margin-top: 1rem; font-weight: 600; }
  input, select { width: 100%; padding: .5rem; margin-top: .25rem; box-sizing: border-box; font: inherit; }
  button { margin-top: 1.25rem; padding: .6rem 1.2rem; font: inherit; cursor: pointer; }
  .hint { font-size: .9rem; color: #555; font-weight: 400; }
  .msg { padding: .75rem 1rem; margin-top: 1rem; border-radius: 6px; }
  .ok { background: #e6f4ea; border: 1px solid #34a853; }
  .err { background: #fce8e6; border: 1px solid #d93025; }
</style>
</head>
<body>
<h1>OCR key settings</h1>
<p>Register the OCR key that should be used for <em>your</em> annotations.
You can come back and resubmit at any time to change it.</p>

{% if message %}
<div class="msg {{ 'ok' if ok else 'err' }}">{{ message }}</div>
{% endif %}

<form method="post" autocomplete="off">
  <label>Your Label Studio token
    <span class="hint">Label Studio &rarr; your avatar (top right) &rarr; Account &amp; Settings &rarr;
    Personal Access Token (or Legacy Token). Used once to prove who you are; it is not stored.</span>
    <input type="password" name="ls_token" required>
  </label>

  <label>OCR provider
    <select name="provider">
      <option value="gemini" {{ 'selected' if provider == 'gemini' }}>Gemini</option>
      <option value="sarvam" {{ 'selected' if provider == 'sarvam' }}>Sarvam</option>
    </select>
  </label>

  <label>Your OCR API key
    <input type="password" name="ocr_key" required>
  </label>

  <button type="submit">Save</button>
</form>
</body>
</html>
"""


def _settings_page(message=None, ok=False, provider="gemini", status=200):
    html = render_template_string(SETTINGS_PAGE, message=message, ok=ok, provider=provider)
    return html, status


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "GET":
        return _settings_page()

    ls_token = (request.form.get("ls_token") or "").strip()
    provider = (request.form.get("provider") or "").strip().lower()
    ocr_key = (request.form.get("ocr_key") or "").strip()

    if not ls_token or not ocr_key:
        return _settings_page("Both the Label Studio token and the OCR key are required.",
                              provider=provider or "gemini", status=400)
    if provider not in PROVIDERS:
        return _settings_page("Pick Gemini or Sarvam.", status=400)

    try:
        user = verify_ls_token(ls_token)
    except LSAuthError as exc:
        # Nothing is written when verification fails.
        return _settings_page(str(exc), provider=provider, status=401)

    save_user_creds(user["id"], provider, ocr_key, user.get("username"))
    log.info("Registered %s key for LS user %s", provider, user["id"])  # never log the key
    return _settings_page(
        f"Saved. Label Studio user {user['id']} will now use your {provider.title()} key for OCR.",
        ok=True, provider=provider,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9090))
    # Debug mode (auto-reload + interactive debugger) is a remote-code-
    # execution risk on a shared network, so it's OFF unless you opt in
    # for local development: FLASK_DEBUG=1 python app.py
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)