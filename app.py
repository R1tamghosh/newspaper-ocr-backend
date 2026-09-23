"""
Newspaper OCR ML backend for Label Studio.

Implements the three HTTP endpoints Label Studio expects from an ML backend:
  GET  /health   -> tells Label Studio the backend is alive
  POST /setup    -> called when you connect the backend to a project
  POST /predict  -> called every time you draw/finish a box (interactive mode)

No label_studio_ml SDK, no Redis, no queue - just Flask, Pillow and the
Gemini API. Easier to read, easier to debug.
"""

import base64
import io
import logging
import os
import tempfile

import requests
from flask import Flask, jsonify, request
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()  # reads .env in the current directory into os.environ

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("newspaper-ocr")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")
SARVAM_LANGUAGE = os.environ.get("SARVAM_LANGUAGE", "bn-IN")

# "gemini" or "sarvam" - which model actually does the transcribing
OCR_PROVIDER = os.environ.get("OCR_PROVIDER", "gemini").lower()

# If Label Studio and this backend both run locally, LS serves task images
# from its own file server and usually needs this auth header to fetch them.
LABEL_STUDIO_URL = os.environ.get("LABEL_STUDIO_URL", "http://localhost:8080")
LABEL_STUDIO_API_KEY = os.environ.get("LABEL_STUDIO_API_KEY", "")

PROMPT = (
    "Transcribe every word of text visible in this newspaper clipping, "
    "exactly as printed, preserving line breaks. Output only the "
    "transcribed text - no commentary, no quotes, no markdown."
)

_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

_sarvam_client = None
if SARVAM_API_KEY:
    from sarvamai import SarvamAI
    _sarvam_client = SarvamAI(api_subscription_key=SARVAM_API_KEY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def ls_headers() -> dict:
    if not LABEL_STUDIO_API_KEY:
        return {}
    # Personal Access Tokens (PATs) are JWT refresh tokens - they can't be
    # used directly. Exchange for a short-lived access token first, then
    # use it as a Bearer token. (Legacy tokens skip this and use
    # "Token <key>" directly - handled below.)
    if LABEL_STUDIO_API_KEY.count(".") == 2:  # looks like a JWT
        access_token = get_ls_access_token()
        return {"Authorization": f"Bearer {access_token}"}
    return {"Authorization": f"Token {LABEL_STUDIO_API_KEY}"}


def get_ls_access_token() -> str:
    """Exchange the PAT (refresh token) for a short-lived access token.
    Called fresh on every request rather than cached, since webhook volume
    here is low and access tokens expire quickly."""
    url = f"{LABEL_STUDIO_URL.rstrip('/')}/api/token/refresh"
    resp = requests.post(url, json={"refresh": LABEL_STUDIO_API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()["access"]


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


SKIP_OCR_LABELS = {"Image"}  # regions with these labels are never sent to Gemini


def ocr_missing_regions(image_ref: str, result: list) -> tuple[list, bool]:
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
        text = ocr_transcribe(crop)
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


def ocr_with_sarvam(crop_img: Image.Image) -> str:
    if _sarvam_client is None:
        return "[SARVAM_API_KEY not set on the ML backend]"

    buf = io.BytesIO()
    crop_img.save(buf, format="PNG")
    buf.seek(0)

    # Step 1: start the job. Note "digitise" (Sarvam's spelling), and the
    # file goes in as a list — a (filename, filelike) tuple works.
    start = _sarvam_client.doc_ai.digitise(
        file=[("crop.png", buf)],
        language=SARVAM_LANGUAGE,
        output_format="md",
    )
    job_id = start.job_id

    # Step 2: poll until the job finishes. digitise() only *starts* the
    # job - it does not return text.
    for _ in range(30):  # ~30s max wait, adjust if needed
        status = _sarvam_client.doc_ai.get_status(job_id)
        if status.status in ("completed", "partially_completed"):
            break
        if status.status in ("failed", "rejected"):
            return f"[Sarvam job {status.status}]"
        time.sleep(1)
    else:
        return "[Sarvam job timed out]"

    # Step 3: fetch the actual text once the job is done.
    results = _sarvam_client.doc_ai.get_results(job_id)
    pieces = []
    for page in getattr(results, "pages", []) or []:
        text = getattr(page, "md", None) or getattr(page, "text", None)
        if text:
            pieces.append(text)
    return "\n".join(pieces).strip()

def ocr_transcribe(crop_img: Image.Image) -> str:
    """Single entry point the rest of the app calls - dispatches to
    whichever provider OCR_PROVIDER is set to."""
    if OCR_PROVIDER == "sarvam":
        return ocr_with_sarvam(crop_img)
    return ocr_with_gemini(crop_img)


def ocr_with_gemini(crop_img: Image.Image) -> str:
    if _client is None:
        return "[GEMINI_API_KEY not set on the ML backend]"

    buf = io.BytesIO()
    crop_img.save(buf, format="PNG")

    response = _client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            PROMPT,
            types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"),
        ],
    )
    return (response.text or "").strip()


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

    # Interactive mode: Label Studio sends the region you just drew in
    # context['result']. If it's missing, there's nothing to OCR yet.
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
        text = ocr_transcribe(crop)
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
    log.info("=== /webhook raw payload ===\n%s", data)

    if action not in ("ANNOTATION_CREATED", "ANNOTATION_UPDATED"):
        return jsonify({"status": "ignored", "action": action}), 200

    annotation = data.get("annotation")
    task = data.get("task")

    # The auto-created ML-backend webhook doesn't always include full data -
    # fall back to fetching it from the API if it's missing.
    if not annotation or "result" not in annotation:
        annotation_id = (annotation or {}).get("id") or data.get("annotation_id")
        if not annotation_id:
            log.error("No annotation id in webhook payload, can't proceed")
            return jsonify({"status": "no annotation id"}), 200
        log.info("Fetching full annotation %s from API", annotation_id)
        annotation = fetch_annotation(annotation_id)

    if not task or "data" not in task:
        task_id = (task or {}).get("id") or annotation.get("task")
        if task_id:
            log.info("Fetching full task %s from API", task_id)
            task = fetch_task(task_id)

    if not task or "data" not in task:
        log.error("Could not resolve task data, giving up")
        return jsonify({"status": "no task data"}), 200

    image_ref = task["data"].get("image") or task["data"].get("img")
    result = annotation.get("result") or []

    try:
        updated_result, changed = ocr_missing_regions(image_ref, result)
    except Exception:
        log.exception("OCR failed while processing webhook")
        return jsonify({"status": "ocr error"}), 200

    if not changed:
        log.info("Nothing to OCR (all regions already transcribed)")
        return jsonify({"status": "no changes"}), 200

    patch_result = patch_annotation(annotation["id"], updated_result)
    log.info("=== patched annotation %s ===", annotation["id"])
    return jsonify({"status": "ok", "annotation": patch_result.get("id")}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9090))
    app.run(host="0.0.0.0", port=port, debug=True)