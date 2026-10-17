# Newspaper OCR Annotation Platform: Setup on a New Device (KT)

Scope: install, configure, expose and operate the full platform on a new host. Covers port changes, data migration, onboarding and failure signatures.

Reference versions: Label Studio 1.23.0, Python 3.10+ (backend), Flask 3.1.x, Windows 11 22H2+ with WSL2 Ubuntu (primary target).

---

## 1. Components

| Component | Runs as | Default port | Purpose |
|---|---|---|---|
| Label Studio | `label-studio start` (venv `vision`) | 8080 | Annotation UI, users, tasks, webhooks |
| OCR backend | `python app.py` (venv `OCRvenv`) | 9090 | `/webhook` (OCR + write-back), `/settings` (per-user key registration), `/health` |
| `user_keys.json` | file next to `app.py` | n/a | `{ "<LS user id>": {provider, api_key, username} }` |
| Tampermonkey userscript | browser extension | n/a | Bengali phonetic typing in Label Studio |
| `ls_to_html.py` | CLI script | n/a | Export JSON to HTML page recreations |

Request flow:

```
Annotator browser -> http://<HOST_IP>:<LS_PORT>            Label Studio
Annotator browser -> http://<HOST_IP>:<API_PORT>/settings   key registration (once)
Label Studio -> http://localhost:<API_PORT>/webhook         on annotation created/updated
Backend: completed_by -> user_keys.json -> OCR with that user's key -> PATCH annotation via LS API
```

---

## 2. Port parameters

`<LS_PORT>` default 8080. `<API_PORT>` default 9090.

**No code change is required to use different ports.** `app.py` reads `PORT` and `LABEL_STUDIO_URL` from the environment. No port number is hardcoded in `app.py`.

Every place a port appears:

| # | Location | Setting |
|---|---|---|
| 1 | Label Studio start command | `label-studio start --port <LS_PORT>` |
| 2 | Backend `.env` | `PORT=<API_PORT>` |
| 3 | Backend `.env` | `LABEL_STUDIO_URL=http://localhost:<LS_PORT>` |
| 4 | Label Studio project, Settings, Webhooks | URL `http://localhost:<API_PORT>/webhook` |
| 5 | Host firewall | Inbound TCP `<LS_PORT>`, `<API_PORT>` |
| 6 | Tampermonkey script header | `@match http://<HOST_IP>:<LS_PORT>/*` and `@match http://localhost:<LS_PORT>/*` |
| 7 | Annotator-facing URLs | `http://<HOST_IP>:<LS_PORT>`, `http://<HOST_IP>:<API_PORT>/settings` |

Code change is required only if Label Studio and the backend run on **different machines**: `app.py` returns 403 for `/webhook`, `/predict`, `/setup` unless the request originates from `127.0.0.1` or `::1`. Adjust `LOOPBACK_ADDRS` in that case.

Check a port is free before starting.

WSL:
```bash
ss -tlnp | grep -E ':<LS_PORT>|:<API_PORT>'
```
Windows PowerShell:
```powershell
netstat -ano | findstr ":<LS_PORT> :<API_PORT>"
```
Expected output: none.

---

## 3. Prerequisites

- Administrator rights on the host.
- Windows 11 22H2 or newer; `wsl --version` reports 2.0.0 or newer (`wsl --update` otherwise).
- Host on a network where client-to-client traffic is permitted (confirm with network admin).
- Static or reserved IP for the host, or a hostname.
- OCR API key per annotator (Gemini or Sarvam).
- Owner Label Studio token (created in section 6).
- Files from the old host (section 11): `.env`, labeling config XML, Tampermonkey script, optionally the Label Studio data directory and `user_keys.json`.

---

## 4. Install

### 4.1 WSL2 + Ubuntu (PowerShell as Administrator)

```powershell
wsl --install -d Ubuntu
wsl --update
```
Reboot if prompted. Create the Linux user when Ubuntu first opens.

### 4.2 Base packages (WSL)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git tmux curl
python3 --version
```
Python must be 3.10 or newer.

### 4.3 Label Studio environment (WSL)

```bash
mkdir -p ~/Dissertation && cd ~/Dissertation
python3 -m venv vision
source vision/bin/activate
pip install --upgrade pip
pip install label-studio==1.23.0
label-studio --version
deactivate
```

### 4.4 Backend environment and code (WSL)

```bash
cd ~/Dissertation
git clone <REPO_URL> newspaper-ocr-backend
cd newspaper-ocr-backend
git checkout master
git tag
python3 -m venv ~/Dissertation/OCRvenv
source ~/Dissertation/OCRvenv/bin/activate
pip install --upgrade pip
pip install flask pillow requests python-dotenv google-genai sarvamai
python -m py_compile app.py && echo OK
deactivate
```
Deploy `master` (contains the multi-user merge) or tag `v4-multiuser-keys`. Do not deploy `v3-bengali-typing` on a network: it has `debug=True` and global keys.

`.gitignore` must contain:
```
.env
user_keys.json
user_keys.json.lock
.user_keys_*
```

---

## 5. Backend configuration

Create `~/Dissertation/newspaper-ocr-backend/.env`:

```
LABEL_STUDIO_URL=http://localhost:<LS_PORT>
LABEL_STUDIO_API_KEY=<owner token>
PORT=<API_PORT>
GEMINI_MODEL=gemini-3.1-flash-lite
SARVAM_LANGUAGE=bn-IN
OCR_PROVIDER=gemini
GEMINI_API_KEY=<optional, /predict only>
SARVAM_API_KEY=<optional, /predict only>
```

| Variable | Used by | Notes |
|---|---|---|
| `LABEL_STUDIO_URL` | all backend calls to Label Studio | Always `localhost` when co-located |
| `LABEL_STUDIO_API_KEY` | backend reads tasks/annotations, PATCHes results | Owner account token. Legacy token (`Token <key>`) or PAT (JWT with two dots, exchanged at `/api/token/refresh`) |
| `PORT` | Flask listen port | Default 9090 |
| `GEMINI_MODEL`, `SARVAM_LANGUAGE` | OCR calls for all users | Global |
| `OCR_PROVIDER`, `GEMINI_API_KEY`, `SARVAM_API_KEY` | dormant `/predict` route only | The webhook never uses these |
| `USER_KEYS_PATH` | optional | Default: `user_keys.json` beside `app.py` |

---

## 6. Label Studio configuration

### 6.1 Start (see section 8) and create the owner account

1. Browse `http://localhost:<LS_PORT>/user/signup`.
2. Create the first account. It becomes user 1 and owns the organization.
3. Avatar (top right), Account & Settings, copy the Legacy Token (or Personal Access Token).
4. Put it in backend `.env` as `LABEL_STUDIO_API_KEY`.

Do **not** set `LABEL_STUDIO_HOST` when starting Label Studio (section 12, row 2).

### 6.2 Create project and labeling interface

1. Projects, Create, name the project.
2. Settings, Labeling Interface, Code view. Paste the original config (preferred: `labeling_config.xml` from the old host).
3. If the original is unavailable, use the reconstruction below. It is derived from the control-tag names and labels in an export, not copied from the original; interface options (hotkeys, rows, colors) are not recoverable.

```xml
<View>
  <Image name="page" value="$image" zoom="true"/>
  <RectangleLabels name="bbox" toName="page">
    <Label value="Headline"/>
    <Label value="Subtitle"/>
    <Label value="BodyText"/>
    <Label value="Caption"/>
    <Label value="Image"/>
  </RectangleLabels>
  <TextArea name="transcription" toName="page" perRegion="true" editable="true"/>
</View>
```

Hard dependencies in the backend:

| Requirement | Why |
|---|---|
| `<TextArea name="transcription" toName="page">` | `app.py` writes OCR text to `from_name="transcription"`, `to_name="page"` |
| `<RectangleLabels name="bbox">` | region boxes |
| `perRegion="true"` on the TextArea | text is paired to its box by shared region id |
| Label `Image` | regions labeled `Image` are never OCR'd (`SKIP_OCR_LABELS`) |
| Task data key `image` (or `img`) | `app.py` reads `task["data"]["image"]` |

An older export contains a legacy label `TextBlock`. Add `<Label value="TextBlock"/>` only to display or edit old data containing it.

### 6.3 Webhook

Project, Settings, Webhooks, Add:
- URL: `http://localhost:<API_PORT>/webhook`
- Events: Annotation created, Annotation updated
- Keep "Send payload" enabled.

### 6.4 Import tasks

Data Manager, Import, upload page images. Imported images are served from `/data/upload/...`.

---

## 7. Network exposure

Run section 7.1 or 7.2 on Windows. Never combine them.

Windows network profile must be Private or Domain, or the firewall rule below does not apply:
```powershell
Get-NetConnectionProfile
```

### 7.1 Windows 11: WSL mirrored networking

1. Create `C:\Users\<user>\.wslconfig`:
   ```
   [wsl2]
   networkingMode=mirrored
   ```
   Create via: `notepad $env:USERPROFILE\.wslconfig`. Confirm name with `dir $env:USERPROFILE\.wslconfig*` (must not end in `.txt`).
2. Confirm no portproxy rules exist (must print nothing):
   ```powershell
   netsh interface portproxy show all
   ```
   If rules are listed:
   ```powershell
   netsh interface portproxy delete v4tov4 listenport=<LS_PORT> listenaddress=0.0.0.0
   netsh interface portproxy delete v4tov4 listenport=<API_PORT> listenaddress=0.0.0.0
   ```
3. PowerShell as Administrator:
   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   New-NetFirewallRule -DisplayName "Newspaper OCR" -Direction Inbound -Protocol TCP -LocalPort <LS_PORT>,<API_PORT> -Action Allow -Profile Private,Domain
   ```
   The GUID is the fixed WSL VM identifier.
4. Apply:
   ```powershell
   wsl --shutdown
   ```
   Reopen WSL.
5. Confirm WSL shows the same IPv4 as Windows (`ipconfig` on Windows, `ip addr` in WSL).

### 7.2 Windows 10 or mirrored mode unavailable: port forwarding

PowerShell as Administrator:
```powershell
$wslIp = (wsl hostname -I).Trim().Split(' ')[0]
netsh interface portproxy add v4tov4 listenport=<LS_PORT> listenaddress=0.0.0.0 connectport=<LS_PORT> connectaddress=$wslIp
netsh interface portproxy add v4tov4 listenport=<API_PORT> listenaddress=0.0.0.0 connectport=<API_PORT> connectaddress=$wslIp
New-NetFirewallRule -DisplayName "Newspaper OCR" -Direction Inbound -Protocol TCP -LocalPort <LS_PORT>,<API_PORT> -Action Allow -Profile Private,Domain
netsh interface portproxy show all
```
The WSL IP changes on restart. Delete and re-add both rules after each change of WSL IP.

### 7.3 Native Linux or macOS host

No WSL steps. Open the ports in the OS firewall:
```bash
sudo ufw allow <LS_PORT>/tcp
sudo ufw allow <API_PORT>/tcp
```
macOS: allow incoming connections for Python when prompted.

### 7.4 Stable address

Obtain a DHCP reservation or hostname for the host. Host IP: `ipconfig` (Windows) IPv4 of the active adapter.

---

## 8. Start and stop

Start order: Label Studio, then backend.

```bash
tmux new -s ls
cd ~/Dissertation && source vision/bin/activate
label-studio start --port <LS_PORT>
# detach: Ctrl+b, d

tmux new -s ocr
cd ~/Dissertation/newspaper-ocr-backend
source ~/Dissertation/OCRvenv/bin/activate
python app.py
# detach: Ctrl+b, d
```

Default ports: omit `--port` for 8080.
Development only: `FLASK_DEBUG=1 python app.py` (never while network-exposed).

Reattach: `tmux attach -t ls`, `tmux attach -t ocr`. List: `tmux ls`.
Stop: reattach, `Ctrl+C`. Stop Label Studio before copying its data directory.

Host settings: Windows power plan sleep = Never while plugged in. Do not run `wsl --shutdown` while annotators are working.

---

## 9. Verification checklist

On the host:

| # | Command | Expected |
|---|---|---|
| 1 | `curl -sI http://localhost:<LS_PORT> \| head -1` | `HTTP/1.1 302 Found` |
| 2 | `curl -s http://localhost:<API_PORT>/health` | JSON with `"status":"UP"` |
| 3 | `curl -i -H "Authorization: Token <legacy token>" http://localhost:<LS_PORT>/api/current-user/whoami` | `200` and JSON with integer `id` |
| 4 | `ss -tlnp \| grep -E ':<LS_PORT>\|:<API_PORT>'` | listeners on `0.0.0.0` or `*` |

From a second device on the same network:

| # | URL | Expected |
|---|---|---|
| 5 | `http://<HOST_IP>:<LS_PORT>` | Label Studio login page |
| 6 | `http://<HOST_IP>:<API_PORT>/settings` | registration form |
| 7 | `curl -i -X POST http://<HOST_IP>:<API_PORT>/webhook` | `403` |

Functional:

| # | Action | Expected |
|---|---|---|
| 8 | Register owner at `/settings` | "Saved. Label Studio user 1 will now use your ... key"; entry in `user_keys.json` |
| 9 | Draw a Headline/BodyText box on a fresh task | backend log `OCR for LS user 1 via <provider> (key ...xxxx)`, then `=== patched annotation N ===` |
| 10 | Two accounts annotate separate fresh tasks | two log lines with different user ids and key suffixes |
| 11 | Unregistered user draws a box | log `No registered OCR key for Label Studio user N`; region stays blank |
| 12 | Wrong key registered | log `OCR failed for region ...`; region stays blank; no crash |

A second log line `Nothing to OCR (all regions already transcribed)` after each OCR is expected (the backend's own PATCH triggers another webhook).

---

## 10. Onboarding an annotator

Operator:
1. Label Studio, menu (top left), Organization, Add People, copy the invite link.
2. If the link starts with `localhost`, replace `localhost` with `<HOST_IP>`.
3. Send the link and the steps below.

Annotator:
1. Open the link, create the account.
2. Avatar, Account & Settings, copy the Legacy Token or Personal Access Token.
3. Open `http://<HOST_IP>:<API_PORT>/settings`.
4. Enter the Label Studio token, choose provider, enter own OCR API key, Save.
5. Annotate **fresh tasks** (tasks with no existing annotation).

Rules:
- OCR uses the key of the user who **created** the annotation (`completed_by`). Editing another user's annotation uses the creator's key.
- Pre-drawn annotations made by the operator run on the operator's key.
- Community Edition has no per-project permissions: every account in the organization can open every project.
- Resubmitting `/settings` replaces that user's key.

Tampermonkey (each annotator, if Bengali phonetic typing is used): install the userscript; header must include `@match http://<HOST_IP>:<LS_PORT>/*`.

---

## 11. Migrating from an existing host

Items to copy:

| Item | Location (old host) | Notes |
|---|---|---|
| Label Studio data | `~/.local/share/label-studio/` | Contains `label_studio.sqlite3`, `media/` (uploaded images), and `.env` (SECRET_KEY). Stop Label Studio first. |
| Backend `.env` | `~/Dissertation/newspaper-ocr-backend/.env` | Update `LABEL_STUDIO_URL`, `PORT`, token |
| `user_keys.json` | beside `app.py` | Copy **only** if the Label Studio data directory was copied (user ids unchanged) |
| Labeling config | project Settings, Labeling Interface, Code | Save as `labeling_config.xml` |
| Tampermonkey script | extension Dashboard, Utilities, Export | Update `@match` |

Procedure with the data directory copied (users, tokens, tasks, annotations preserved):
1. Old host: stop Label Studio and backend. Archive the data directory: `tar czf ls-data.tgz -C ~/.local/share label-studio`.
2. Transfer `ls-data.tgz`, `.env`, `user_keys.json`.
3. New host: complete sections 4 and 5. Extract: `tar xzf ls-data.tgz -C ~/.local/share`. Place `.env` and `user_keys.json` in the backend folder.
4. Update ports and `LABEL_STUDIO_API_KEY` if needed (legacy tokens in the copied database remain valid).
5. Start (section 8). Recheck the webhook URL in project settings (section 6.3).
6. Run section 9.

Procedure with a fresh database (new user ids):
1. Old host: Data Manager, Export, JSON (annotations backup). Keep `labeling_config.xml`.
2. New host: section 6 from scratch. Import images, then re-import annotations if needed.
3. Do **not** copy `user_keys.json`. Every annotator re-registers at `/settings`.

---

## 12. Failure signatures

| # | Symptom | Cause | Resolution |
|---|---|---|---|
| 1 | `Address already in use` or "Port in use by another program"; `netstat` shows `svchost.exe` with service `iphlpsvc` on the port | Leftover `netsh interface portproxy` rules | `netsh interface portproxy show all`; delete the rules (7.1 step 2); `Restart-Service iphlpsvc` if the port stays held. Do not kill the svchost process. Check with `tasklist /svc /FI "PID eq <pid>"` |
| 2 | Blank white page on the host PC; console shows `ERR_CONNECTION_TIMED_OUT` for `http://<HOST_IP>:<LS_PORT>/static/...`; other devices work | Label Studio started with `LABEL_STUDIO_HOST=http://<HOST_IP>:<LS_PORT>`; page assets point at the IP, which the host cannot reach through WSL | Restart with `label-studio start` only (no `LABEL_STUDIO_HOST`). Use `localhost` on the host, `<HOST_IP>` elsewhere. Replace `localhost` in invite links manually |
| 3 | Other device cannot load either page | Firewall, network profile Public, mirrored mode not applied, or client isolation on the network | Section 7: `Get-NetConnectionProfile`, firewall rule, Hyper-V firewall setting, `wsl --shutdown`; confirm with network admin |
| 4 | Annotation by user 2 logged as user 1 | Annotation was created by user 1; user 2 edited it | Use a fresh task, or start a new annotation (+ next to annotation tabs) |
| 5 | Log: `No registered OCR key for Label Studio user N` | User not registered, or registered under another account | User opens `/settings` while logged into the correct Label Studio account |
| 6 | Log: `OCR failed for region ... ` | Wrong, revoked or out-of-quota key; provider error | User resubmits `/settings` with a working key |
| 7 | Log: `Blocked POST /webhook from non-local address` for Label Studio's own calls | Label Studio not on the backend's machine (container or other host) | Run co-located, or edit `LOOPBACK_ADDRS` in `app.py` |
| 8 | Box drawn, nothing happens, no webhook log line | Webhook missing, wrong URL or port | Section 6.3 |
| 9 | `/settings`: "rejected that token" | Wrong or truncated token | Re-copy the full token; try the Legacy Token |
| 10 | `/settings`: "no user endpoint responded as expected" | Label Studio user endpoint unavailable on this version | Run verification row 3 |
| 11 | `database is locked` | SQLite with concurrent writers | Move Label Studio to PostgreSQL |
| 12 | Gio: `Operation not supported` at Label Studio start | WSL has no browser to auto-open | Ignore; open the URL in the Windows browser |
| 13 | `Can't get latest version` traceback (pypi.org timeout) at Label Studio start | Update check timed out | Ignore; startup continues |
| 14 | `RuntimeWarning: Accessing the database during app initialization` | Django startup warning | Ignore |
| 15 | Browser Issues panel: CSP "Report-only" entries | Label Studio's report-only policy | Ignore |

---

## 13. Operations

Logs (backend terminal):

| Line | Meaning |
|---|---|
| `Registered <provider> key for LS user N` | successful registration |
| `OCR for LS user N via <provider> (key ...xxxx)` | OCR started with that user's key (last 4 characters) |
| `=== patched annotation N ===` | transcription written back |
| `No registered OCR key for Label Studio user N` | OCR skipped |
| `OCR failed for region ...` | region left blank |
| `Blocked <method> <path> from non-local address <ip>` | internal route requested from the network |

User keys: edit `user_keys.json` (re-read on every request; no restart). Deleting an entry disables OCR for that user. The file is created with mode 600 and written atomically under a file lock.

Backups:
- `~/.local/share/label-studio/` while Label Studio is stopped, or periodic JSON exports from the project (Export button).
- `user_keys.json` and `.env` to private storage only (contain secrets). Never commit.

Upgrade backend: `git pull`, restart `python app.py`.
Upgrade Label Studio: `pip install -U label-studio` inside `vision`; back up the data directory first.

Scale: for sustained parallel use run `gunicorn -w 1 --threads 8 -b 0.0.0.0:<API_PORT> app:app` and move Label Studio to PostgreSQL.

Security:
- Traffic is plain HTTP; Label Studio logins and `/settings` submissions are not encrypted on the network.
- `FLASK_DEBUG` unset on any network-exposed host.
- `.env` and `user_keys.json` contain live secrets.
- Only `/`, `/health`, `/settings` are reachable from other machines.

---

## 14. HTML export (ground truth pages)

```bash
python ls_to_html.py <label_studio_export>.json -o html_pages
```
Input: Label Studio JSON export, a list of tasks. Output: `page_<task>_<image>.html` per task and `index.html`. Standard library only.

Export JSON structure: list of tasks; `annotations[].result` is a flat list where each region is two entries sharing an `id` (`rectanglelabels`/`bbox` and `textarea`/`transcription`); coordinates are percentages of `original_width` and `original_height`.

---

## 15. File locations

| Item | Path |
|---|---|
| Label Studio environment | `~/Dissertation/vision/` |
| Backend environment | `~/Dissertation/OCRvenv/` |
| Backend code | `~/Dissertation/newspaper-ocr-backend/app.py` |
| Backend config | `~/Dissertation/newspaper-ocr-backend/.env` |
| Key store | `~/Dissertation/newspaper-ocr-backend/user_keys.json` |
| Label Studio data | `~/.local/share/label-studio/` |
| WSL network config | `C:\Users\<user>\.wslconfig` |
