// Shared across every page -- include this <script> before any page-specific
// script. Point API_BASE at wherever api.py is running (defaults to local).
const API_BASE = window.JOB_AGENT_API_BASE || "http://localhost:8000";

async function getJSON(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function postJSON(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> ${res.status}`);
  return data;
}

async function deleteJSON(path) {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> ${res.status}`);
  return data;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// The status pill lives in the shared nav header on every page.
async function loadNavStatus() {
  const pill = document.getElementById("status-pill");
  if (!pill) return;
  try {
    const status = await getJSON("/api/status");
    if (status.dry_run) {
      pill.textContent = "Dry run — no live actions";
      pill.className = "pill pill-warn";
    } else {
      pill.textContent = "Agent active";
      pill.className = "pill pill-ok";
    }
  } catch (e) {
    pill.textContent = "Backend unreachable";
    pill.className = "pill pill-muted";
  }
}

document.addEventListener("DOMContentLoaded", loadNavStatus);
