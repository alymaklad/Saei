# Deploying to Google Cloud (free)

Runs `api.py` and `scheduler.py` exactly as they are in the repo root, on a
Compute Engine **e2-micro** instance under Google Cloud's Always Free tier
(one instance/month, forever, not a trial credit). The frontend stays on
Cloudflare Pages; Cloudflare Tunnel gives the API a free public HTTPS URL
without opening any port on the VM.

## 1. Create the VM

Requires the `gcloud` CLI, authenticated (`gcloud auth login`) against your project.

```bash
gcloud compute instances create job-agent-vm \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard \
  --metadata-from-file=startup-script=deploy/gcp/startup-script.sh
```

Must be `us-west1`, `us-central1`, or `us-east1` and `e2-micro` — that's the
combination the Always Free tier covers. The startup script installs Python,
clones this repo to `/opt/job-agent`, sets up a venv, installs
`requirements.txt`, and registers `job-agent-api` + `job-agent-scheduler` as
systemd services (auto-restart on crash, auto-start on reboot).

If the repo is private, add a deploy key or personal access token to the
`REPO_URL` in `startup-script.sh` before creating the VM — an unauthenticated
`git clone` will fail on a private repo.

## 2. Fill in real secrets

```bash
gcloud compute ssh job-agent-vm --zone=us-central1-a
sudo nano /opt/job-agent/.env
```

Two things specific to this VM:
- **LLM_PROVIDER** — the startup script already switches this to `gemini`
  (e2-micro's 1GB RAM can't run Ollama). Set `GEMINI_API_KEY` to a free key
  from [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
- **Credentials files** (Gmail OAuth, Google service account for the Sheets
  watchlist) — upload them with `gcloud compute scp`:

```bash
gcloud compute scp credentials/gmail_credentials.json job-agent-vm:/opt/job-agent/credentials/ --zone=us-central1-a
gcloud compute scp credentials/service_account.json job-agent-vm:/opt/job-agent/credentials/ --zone=us-central1-a
```

Then restart both services to pick up the changes:

```bash
sudo systemctl restart job-agent-api job-agent-scheduler
sudo systemctl status job-agent-api job-agent-scheduler
```

## 3. Give the API a free public HTTPS URL

`api.py` binds to `127.0.0.1` only — nothing is exposed to the internet
until you wire up a tunnel. On the VM:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb
cloudflared tunnel login
cloudflared tunnel create job-agent
cloudflared tunnel route dns job-agent api.yourdomain.com   # any domain/subdomain on your Cloudflare account
```

Copy `deploy/gcp/cloudflared/config.yml.example` to `/etc/cloudflared/config.yml`,
fill in the `<TUNNEL_ID>` and hostname it just printed, then:

```bash
sudo cp /opt/job-agent/deploy/gcp/cloudflared/config.yml.example /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml   # fill in TUNNEL_ID + your hostname
sudo cloudflared service install
sudo systemctl start cloudflared
```

`https://api.yourdomain.com` now reaches the API — no firewall rule was ever opened.

## 4. Point the frontend at it

Edit `frontend/app.js`:

```js
const API_BASE = window.JOB_AGENT_API_BASE || "https://api.yourdomain.com";
```

Redeploy to Cloudflare Pages (or whichever static host you used).

## Notes

- Both services auto-restart on failure and on VM reboot (`systemctl enable`
  in the startup script) — this is what makes the daily/weekly triggers run
  "automatically" the way the original design called for.
- Re-running `gcloud compute instances add-metadata ... --metadata-from-file
  startup-script=...` and rebooting the VM re-runs the startup script safely
  (it's idempotent) — useful after pulling new code.
- To check the daily/weekly jobs are actually firing: `journalctl -u job-agent-scheduler -f`.
- Free tier is one e2-micro instance per billing account, not per VM you happen
  to name this — don't spin up a second one and expect it to also be free.
