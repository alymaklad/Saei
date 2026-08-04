const connectBtn = document.getElementById("connect-btn");
const connectMessage = document.getElementById("connect-message");
const gmailStatusEl = document.getElementById("gmail-status");
const applicationSelect = document.getElementById("application-select");
const sendMessage = document.getElementById("send-message");
const sendBtn = document.getElementById("send-btn");

async function loadGmailStatus() {
  try {
    const s = await getJSON("/api/email/status");
    if (s.authenticated) {
      gmailStatusEl.textContent = "Connected — Gmail is authorized to send.";
      connectBtn.textContent = "Reconnect";
    } else if (!s.credentials_configured) {
      gmailStatusEl.textContent = "Not set up — no OAuth client file found yet (see note below).";
      connectBtn.textContent = "Connect Gmail";
    } else {
      gmailStatusEl.textContent = "Not connected yet.";
      connectBtn.textContent = "Connect Gmail";
    }
  } catch (e) {
    gmailStatusEl.textContent = "Could not reach the backend API.";
  }
}

connectBtn.addEventListener("click", async () => {
  connectBtn.disabled = true;
  connectMessage.textContent = "Opening a browser tab for Google's consent screen…";
  connectMessage.className = "save-message";
  try {
    await postJSON("/api/email/authenticate", {});
    connectMessage.textContent = "Connected.";
    connectMessage.className = "save-message save-success";
    await loadGmailStatus();
  } catch (e) {
    connectMessage.textContent = e.message || "Authentication failed.";
    connectMessage.className = "save-message save-error";
  } finally {
    connectBtn.disabled = false;
  }
});

async function loadApplicationOptions() {
  try {
    const apps = await getJSON("/api/applications?status=pending_review&limit=100");
    if (!apps.length) {
      applicationSelect.innerHTML = `<option value="">No drafted applications awaiting review</option>`;
      sendBtn.disabled = true;
      return;
    }
    applicationSelect.innerHTML = apps.map((a) => `
      <option value="${a.id}" data-title="${escapeHtml(a.title || "")}" data-company="${escapeHtml(a.company || "")}" data-url="${escapeHtml(a.url || "")}">
        ${escapeHtml(a.title || "Untitled role")} — ${escapeHtml(a.company || "Unknown company")}
      </option>
    `).join("");
    prefillFromSelection();
  } catch (e) {
    applicationSelect.innerHTML = `<option value="">Could not reach the backend API</option>`;
    sendBtn.disabled = true;
  }
}

function prefillFromSelection() {
  const opt = applicationSelect.options[applicationSelect.selectedIndex];
  if (!opt) return;
  const title = opt.dataset.title || "this role";
  const company = opt.dataset.company || "your company";
  document.getElementById("subject").value = `Application for ${title} at ${company}`;
  document.getElementById("body").value =
    `Hello,\n\nI'd like to apply for the ${title} position at ${company}. My CV is attached.\n\nBest regards,`;
}

applicationSelect.addEventListener("change", prefillFromSelection);

async function loadEmails() {
  const body = document.getElementById("emails-body");
  const count = document.getElementById("emails-count");
  try {
    const emails = await getJSON("/api/emails?limit=100");
    count.textContent = `${emails.length} shown`;
    if (!emails.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty">No emails sent yet.</td></tr>`;
      return;
    }
    body.innerHTML = emails.map((e) => `
      <tr>
        <td>${escapeHtml(e.to_email)}</td>
        <td>${escapeHtml(e.subject)}</td>
        <td>${escapeHtml(e.job_title || "—")}${e.company ? ` — ${escapeHtml(e.company)}` : ""}</td>
        <td><span class="status-tag status-${e.status}">${e.status}${e.dry_run ? " (dry run)" : ""}</span></td>
        <td>${e.sent_at ? new Date(e.sent_at).toLocaleString() : "—"}</td>
      </tr>
    `).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="5" class="empty">Could not reach the backend API.</td></tr>`;
  }
}

document.getElementById("send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const applicationId = applicationSelect.value;
  if (!applicationId) return;

  sendBtn.disabled = true;
  sendMessage.textContent = "Sending…";
  sendMessage.className = "save-message";

  try {
    const result = await postJSON(`/api/applications/${applicationId}/send-email`, {
      to_email: document.getElementById("to-email").value,
      subject: document.getElementById("subject").value,
      body: document.getElementById("body").value,
    });
    sendMessage.textContent = result.status === "dry_run" ? "Logged (dry run — not actually sent)." : "Sent.";
    sendMessage.className = "save-message save-success";
    document.getElementById("to-email").value = "";
    await Promise.all([loadApplicationOptions(), loadEmails()]);
  } catch (err) {
    sendMessage.textContent = err.message || "Failed to send.";
    sendMessage.className = "save-message save-error";
  } finally {
    sendBtn.disabled = false;
  }
});

loadGmailStatus();
loadApplicationOptions();
loadEmails();
