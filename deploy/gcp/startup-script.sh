#!/bin/bash
# Compute Engine startup script for the Job Application Agent, sized for the
# Always Free e2-micro instance (us-west1 / us-central1 / us-east1 only).
#
# Idempotent — safe to re-run (it reruns automatically on every VM restart
# since it's registered as instance metadata). Never overwrites .env once
# it exists, so your real secrets survive restarts/redeploys.
set -euo pipefail

APP_DIR="/opt/job-agent"
REPO_URL="https://github.com/alymaklad/Job_Application_Agent.git"
RUN_USER="job-agent"

apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

id -u "$RUN_USER" &>/dev/null || useradd -m -s /usr/sbin/nologin "$RUN_USER"

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/.env"
  echo ">>> Created $APP_DIR/.env from .env.example. Edit it with real values, then:"
  echo ">>>   sudo systemctl restart job-agent-api job-agent-scheduler"
fi

# e2-micro has only 1GB RAM -- Ollama won't run here. Force the free Gemini
# API tier instead unless the operator already changed this by hand.
if grep -q '^LLM_PROVIDER=ollama' "$APP_DIR/.env" 2>/dev/null; then
  sed -i 's/^LLM_PROVIDER=ollama/LLM_PROVIDER=gemini/' "$APP_DIR/.env"
  echo ">>> e2-micro can't run Ollama (1GB RAM) -- switched LLM_PROVIDER to gemini in .env."
  echo ">>> Set GEMINI_API_KEY in $APP_DIR/.env before starting the services."
fi

mkdir -p /etc/systemd/system
cp "$APP_DIR/deploy/gcp/systemd/job-agent-api.service" /etc/systemd/system/
cp "$APP_DIR/deploy/gcp/systemd/job-agent-scheduler.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable job-agent-api job-agent-scheduler
systemctl restart job-agent-api job-agent-scheduler

echo ">>> Startup script complete. Check status with:"
echo ">>>   systemctl status job-agent-api job-agent-scheduler"
