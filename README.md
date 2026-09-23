# Newspaper OCR annotation backend (Label Studio + Gemini)

This is the ML backend half of the setup. Label Studio calls it every time
you finish drawing a box; it crops that box out of the scanned page, sends
the crop to Gemini, and sends the transcribed text back to fill in the
text field next to your box.

No Docker required. Everything below runs as plain Python processes on
your machine. (If you don't know what a Dockerfile is: it's just a recipe
for packaging an app into a portable container image — useful later for
deployment, not needed to get this working locally.)

## 1. Install Label Studio itself (separate from this backend)

```bash
python3 -m venv ls-env
source ls-env/bin/activate      # on Windows: ls-env\Scripts\activate
pip install label-studio
label-studio start
```

This opens Label Studio at `http://localhost:8080`. Create an account,
then create a new Project.

## 2. Set the labeling config

In your new project: **Settings -> Labeling Interface -> Code**, paste in
the contents of `labeling_config.xml` from this folder, and save.

## 3. Upload your newspaper scans

**Settings -> Cloud Storage** (or just drag-and-drop images on the Import
screen) — upload the scanned page images as your tasks. Each task's data
field should be called `image`, matching `$image` in the config.

## 4. Set up this backend (separate terminal, separate venv)

```bash
cd newspaper-ocr-backend
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste in your real Gemini API key
```

Get a Gemini API key at https://aistudio.google.com/apikey if you don't
have one yet.

Load the env file and start the backend:

```bash
export $(cat .env | xargs)      # on Windows, set each var manually instead
python3 app.py
```

You should see it listening on `http://localhost:9090`. Leave this
terminal running.

Sanity check in a third terminal:

```bash
curl http://localhost:9090/health
# {"status": "UP", "model": "gemini-2.5-flash"}
```

## 5. Connect the backend to your Label Studio project

In Label Studio: **Settings -> Machine Learning -> Add Model**

- URL: `http://localhost:9090`
- Leave the rest as default, click **Validate and Save**

Then, critically, turn on **"Retrieve predictions when creating
annotations"** (sometimes called interactive pre-annotation) in the same
ML settings panel — without this, Label Studio won't call your backend
as you draw.

## 6. Annotate

Open a task, draw a rectangle around a block of newspaper text, and
finish the box (click away / press Enter). Within a couple seconds the
`transcription` text box tied to that region should fill in with
Gemini's OCR output. Edit it by hand if Gemini got anything wrong, then
submit the annotation as normal.

## If it's not working

- `curl http://localhost:9090/health` from the machine running Label
  Studio — if this fails, Label Studio can't reach it either (check host/
  port, firewall, or that you're using `http://localhost:9090` and not
  `https://`).
- Check the terminal running `app.py` for a Python traceback — every
  `/predict` call logs errors there.
- If the text field never fills in at all, double check "Retrieve
  predictions when creating annotations" is actually toggled on, and that
  your labeling config's `TextArea` has `perRegion="true"` and matches
  `name="transcription"` / `toName="page"` exactly as in
  `labeling_config.xml`.
- If Label Studio is running as a separate Docker container (not the pip
  install above) and this backend runs on your host machine, `localhost`
  inside the container won't reach your host — use your machine's LAN IP
  instead of `localhost` when adding the model URL, or run both in
  Docker on the same network.

## Files in this folder

- `app.py` — the whole backend: 3 routes (`/health`, `/setup`, `/predict`),
  no framework beyond Flask, no Redis/queue dependency.
- `requirements.txt` — pip dependencies.
- `labeling_config.xml` — paste into Label Studio's labeling interface.
- `.env.example` — copy to `.env` and fill in your Gemini key.
