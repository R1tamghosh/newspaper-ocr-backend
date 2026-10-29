# Newspaper OCR Backend for Label Studio

Flask backend that automatically transcribes newspaper regions annotated in Label Studio. When an annotation is saved, the backend OCRs every region that has no text yet (Gemini or Sarvam) and writes the transcription back into the annotation. Each annotator uses their own OCR API key.

```
Label Studio --webhook--> backend /webhook --> OCR provider (annotator's own key)
                                  \--> PATCH transcription back into the annotation
Annotator --> backend /settings   (one-time key registration)
```

## Requirements

- Python 3.13
- Label Studio 1.23.0
- A Gemini or Sarvam API key for each annotator
- Multi-user hosting: Windows 11 22H2+ with WSL2 (Ubuntu), or a Linux/macOS host

Default ports: Label Studio `8080`, backend `9090`. See [Configuration](#configuration) to change them. No code change is needed.

## Repository layout

| File | Purpose |
|---|---|
| `app.py` | Backend (`/webhook`, `/settings`, `/health`) |
| `labeling_config.xml` | Label Studio labeling interface |
| `requirements.txt` | Backend dependencies |
| `.env.example` | Backend configuration template |
| `ls_to_html.py` | Converts a Label Studio JSON export into HTML page recreations |
| `docs/KT_PLATFORM_SETUP.md` | Full reference: migration, alternative network setups, troubleshooting |

---

## Part 1: Single-user setup

For one annotator working on the same machine as the server.

### 1. Install Label Studio

```bash
python3 -m venv ls-env
source ls-env/bin/activate
pip install label-studio==1.23.0
label-studio start
```

Open `http://localhost:8080`, create an account, then copy your token from **avatar (top right) > Account & Settings** (Legacy Token or Personal Access Token). Stop Label Studio with `Ctrl+C` inside the terminal and deactivate the environment by typing deactivate in the terminal.

### 2. Install the backend

```bash
git clone <https://github.com/R1tamghosh/newspaper-ocr-backend> newspaper-ocr-backend
cd newspaper-ocr-backend
python3 -m venv backend-env
source backend-env/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
LABEL_STUDIO_URL=http://localhost:8080
LABEL_STUDIO_API_KEY=<your Label Studio token>
PORT=9090
```

OCR API keys are not stored in `.env`; they are registered in step 5.

### 3. Start both services

Terminal 1:
```bash
source ls-env/bin/activate
label-studio start
```

Terminal 2:
```bash
cd newspaper-ocr-backend
source backend-env/bin/activate
python app.py
```

Verify: `curl http://localhost:9090/health` returns `"status":"UP"`.

### 4. Configure the Label Studio project

1. **Create Project**, then upload page images under **Data Import**.
2. **Settings > Labeling Interface > Code**: paste the contents of `labeling_config.xml`, then **Save**.
3. **Settings > Webhooks > Add Webhook**:
   - URL: `http://localhost:9090/webhook`
   - Events: **Annotation created** and **Annotation updated**

### 5. Register your OCR key

Open `http://localhost:9090/settings` and enter:

- your Label Studio token (used once to verify your identity, not stored)
- OCR provider (Gemini or Sarvam)
- your OCR API key

Expected result: "Saved. Label Studio user N will now use your ... key for OCR."

### 6. Annotate

1. Open a task and draw a rectangle with a label.
2. The transcription fills in automatically. Correct it if needed, then **Submit**.
3. Regions labeled `Image` are never transcribed.

If a region stays empty, check the backend log: the key is missing or failing.

---

## Part 2: Multi-user setup

One server device hosts Label Studio and the backend. Annotators use a browser only.

### 2A. Server device

1. Complete **Part 1, steps 1 to 4** on the server.
2. Register the owner account (Part 1, step 5).
3. Expose the services on the network (Windows 11 with WSL2):

   a. Confirm no port-forwarding rules exist (output must be empty). PowerShell:
   ```powershell
   netsh interface portproxy show all
   ```

   b. Create `C:\Users\<user>\.wslconfig`:
   ```
   [wsl2]
   networkingMode=mirrored
   ```

   c. PowerShell as Administrator:
   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   New-NetFirewallRule -DisplayName "Newspaper OCR" -Direction Inbound -Protocol TCP -LocalPort 8080,9090 -Action Allow -Profile Private,Domain
   wsl --shutdown
   ```

   d. Reopen WSL and start both services as in Part 1, step 3. Start Label Studio with `label-studio start` only; do not set `LABEL_STUDIO_HOST`.

   e. Find the server IP (`ipconfig`, IPv4 of the active adapter). Request a reserved IP or hostname from the network administrator.

   Other platforms (Windows 10, Linux, macOS): see `docs/KT_PLATFORM_SETUP.md`, section 7.

4. Verify from another device on the same network:

   | URL | Expected |
   |---|---|
   | `http://<HOST_IP>:8080` | Label Studio login page |
   | `http://<HOST_IP>:9090/settings` | Registration form |

   `http://<HOST_IP>:9090/webhook` is restricted to the server itself and returns 403 from other devices.

5. Keep the server running: set Windows sleep to **Never** while plugged in, and keep the Label Studio and backend terminals open (or run them in `tmux`).

### Adding an annotator

1. In Label Studio: menu (top left) > **Organization > Add People**, copy the invite link.
2. If the link begins with `localhost`, replace `localhost` with `<HOST_IP>`.
3. Send the link and the instructions in 2B.

### 2B. Annotator guide

1. Open the invite link and create an account.
2. Go to **avatar (top right) > Account & Settings** and copy your token (Legacy Token or Personal Access Token).
3. Open `http://<HOST_IP>:9090/settings`. Enter your token, choose the provider, enter your own OCR API key, and press **Save**.
4. Open `http://<HOST_IP>:8080`, select the project, and annotate **tasks that have no annotation yet**: draw a box, wait for the transcription, correct it, and **Submit**.

Rules:

- OCR uses the key of the user who **created** the annotation. Editing an annotation created by someone else uses the creator's key.
- To change your key or provider, submit `/settings` again.
- Users who have not registered get no automatic transcription; regions stay blank for manual entry.

---

## Configuration

Backend `.env`:

| Variable | Default | Description |
|---|---|---|
| `LABEL_STUDIO_URL` | `http://localhost:8080` | Label Studio address as seen from the backend |
| `LABEL_STUDIO_API_KEY` | none | Owner account token; the backend uses it to read and update annotations |
| `PORT` | `9090` | Backend port |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini model used for all annotators |
| `SARVAM_LANGUAGE` | `bn-IN` | Sarvam language code |
| `USER_KEYS_PATH` | `user_keys.json` beside `app.py` | Key store location |

Changing ports (no code change):

| Setting | Where |
|---|---|
| Label Studio port | `label-studio start --port <LS_PORT>` |
| Backend port | `PORT` in `.env` |
| Label Studio address | `LABEL_STUDIO_URL` in `.env` |
| Webhook URL | Project Settings > Webhooks: `http://localhost:<API_PORT>/webhook` |
| Firewall | Inbound TCP `<LS_PORT>` and `<API_PORT>` |

Required names in the labeling interface (used by `app.py`): `<TextArea name="transcription" toName="page" perRegion="true">`, `<RectangleLabels name="bbox">`, label `Image` (skipped by OCR), task data key `image`. Use `labeling_config.xml` unchanged.

## Operations

- **Key store:** `user_keys.json` (created beside `app.py`, mode 600) maps Label Studio user IDs to provider and key. Delete an entry to disable OCR for that user. Changes apply without restart.
- **Backend log lines:** `OCR for LS user N via <provider> (key ...xxxx)` confirms which user's key was used; `No registered OCR key for Label Studio user N` means the user must register; `OCR failed for region ...` means the key is invalid, out of quota, or the provider failed.
- **Backups:** the Label Studio data directory (`~/.local/share/label-studio/`, stop Label Studio first) and periodic JSON exports from the project. Back up `.env` and `user_keys.json` privately.
- **Update:** `git pull`, then restart `python app.py`.

## Export to HTML

```bash
python ls_to_html.py <export>.json -o html_pages
```

Reads a Label Studio JSON export and writes one HTML page per task (regions positioned from the bounding-box coordinates), plus `index.html`. Requires only the Python standard library.

## Security

- Traffic is plain HTTP. Use only on a trusted network and with revocable API keys.
- `.env` and `user_keys.json` contain secrets and are excluded from version control.
- Only `/`, `/health` and `/settings` are reachable from other machines; `/webhook`, `/predict` and `/setup` accept local requests only.
- Do not run with `FLASK_DEBUG=1` on a network-exposed host.
