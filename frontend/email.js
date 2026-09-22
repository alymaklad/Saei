// Email page: Gmail connection, the pending-review queue, sent history and
// the composer that sends one application email.

const connectBtn = document.getElementById("connect-btn");
const connectMessage = document.getElementById("connect-message");
const gmailStatusEl = document.getElementById("gmail-status");
const gmailChip = document.getElementById("gmail-chip");
const applicationSelect = document.getElementById("application-select");
const sendMessage = document.getElementById("send-message");
const sendBtn = document.getElementById("send-btn");

let pendingApps = [];
let appsById = {};
let emails = [];
let queueTab = "pending";

// ---- Gmail connection -----------------------------------------------------

async function loadGmailStatus() {
  try {
    const s = await getJSON("/api/email/status");
    if (s.authenticated) {
      gmailStatusEl.textContent = "Connected — authorized to send";
      gmailChip.className = "sa-chip-teal";
      gmailChip.innerHTML = `<span class="h-1.5 w-1.5 rounded-full bg-secondary"></span>Dispatcher active`;
      connectBtn.innerHTML = `<span class="ms text-[18px]">sync</span>Reconnect`;
    } else if (!s.credentials_configured) {
      gmailStatusEl.textContent = "Not set up — no OAuth client file yet (see below)";
      gmailChip.className = "sa-chip-amber";
      gmailChip.textContent = "Not set up";
      connectBtn.innerHTML = `<span class="ms text-[18px]">link</span>Connect Gmail`;
    } else {
      gmailStatusEl.textContent = "Not connected yet";
      gmailChip.className = "sa-chip-amber";
      gmailChip.textContent = "Not connected";
      connectBtn.innerHTML = `<span class="ms text-[18px]">link</span>Connect Gmail`;
    }
  } catch (e) {
    gmailStatusEl.textContent = "Could not reach the backend API.";
    gmailChip.className = "sa-chip";
    gmailChip.textContent = "Offline";
  }
}

connectBtn.addEventListener("click", async () => {
  connectBtn.disabled = true;
  connectMessage.textContent = "Opening a browser tab for Google's consent screen…";
  connectMessage.className = "save-message -mt-4";
  try {
    await postJSON("/api/email/authenticate", {});
    connectMessage.textContent = "Connected.";
    connectMessage.className = "save-message save-success -mt-4";
    await loadGmailStatus();
  } catch (e) {
    connectMessage.textContent = e.message || "Authentication failed.";
    connectMessage.className = "save-message save-error -mt-4";
  } finally {
    connectBtn.disabled = false;
  }
});

// ---- composer ------------------------------------------------------------------

function selectedApp() {
  return appsById[applicationSelect.value] || null;
}

function prefillFromSelection() {
  const a = selectedApp();
  const atsChip = document.getElementById("composer-ats");
  const when = document.getElementById("composer-when");
  const attachment = document.getElementById("attachment");
  const posting = document.getElementById("open-posting");
  renderQueue();
  if (!a) {
    atsChip.hidden = true;
    when.textContent = "";
    attachment.innerHTML = `<p class="sa-empty py-2">No drafted application selected.</p>`;
    posting.hidden = true;
    return;
  }
  const title = a.title || "this role";
  const company = a.company || "your company";
  document.getElementById("subject").value = `Application for ${title} at ${company}`;
  document.getElementById("body").value =
    `Hello,\n\nI'd like to apply for the ${title} position at ${company}. My CV is attached.\n\nBest regards,`;

  const tailored = hasTailored(a);
  const ats = pct(tailored ? a.tailored_ats_score : a.ats_score);
  atsChip.hidden = ats === null;
  atsChip.textContent = `ATS score ${ats}%`;
  when.innerHTML = `<span class="ms text-[16px]">schedule</span>Drafted ${timeAgo(a.date_created)}`;
  posting.hidden = !a.url;
  posting.href = a.url || "#";

  // Mirrors the send endpoint in api.py: it attaches the application's own
  // CV version when one was written, otherwise the master CV.
  const tailoredUrl = cvDownloadUrl(a.cv_path);
  attachment.innerHTML = `
    <div class="flex h-10 w-10 items-center justify-center rounded-xl bg-primary-fixed/60 text-primary"><span class="ms">picture_as_pdf</span></div>
    <div class="min-w-0 flex-1">
      <span class="block truncate text-label-lg text-on-surface">${tailoredUrl ? "Tailored CV for this role" : "Your master CV"}</span>
      <span class="text-label-sm text-on-surface-variant">${ats !== null ? `ATS ${ats}% match` : "Attached automatically"}</span>
    </div>
    ${tailoredUrl ? `<a href="${API_BASE}${tailoredUrl}" target="_blank" rel="noopener" class="sa-icon-btn hover:bg-surface-container-high hover:text-on-surface" aria-label="Open attachment"><span class="ms text-[18px]">download</span></a>` : ""}`;
}

applicationSelect.addEventListener("change", prefillFromSelection);

async function loadApplicationOptions() {
  try {
    const apps = await getJSON("/api/applications?status=pending_review&limit=100");
    pendingApps = sortApplicationsBy(apps, "match_score");
    appsById = {};
    pendingApps.forEach((a) => { appsById[a.id] = a; });
    document.getElementById("stat-drafts").textContent = pendingApps.length;
    if (!pendingApps.length) {
      applicationSelect.innerHTML = `<option value="">No drafted applications awaiting review</option>`;
      sendBtn.disabled = true;
    } else {
      applicationSelect.innerHTML = pendingApps.map((a) => `
        <option value="${a.id}">${escapeHtml(a.title || "Untitled role")} — ${escapeHtml(a.company || "Unknown company")}</option>
      `).join("");
      sendBtn.disabled = false;
      // ?application=123 deep link from the Dashboard / Applications pages.
      const wanted = new URLSearchParams(location.search).get("application");
      if (wanted && appsById[wanted]) applicationSelect.value = wanted;
    }
    prefillFromSelection();
  } catch (e) {
    applicationSelect.innerHTML = `<option value="">Could not reach the backend API</option>`;
    sendBtn.disabled = true;
    renderQueue();
  }
}

// ---- queue (pending drafts / sent / failed) ------------------------------------

function pendingCard(a) {
  const selected = String(a.id) === applicationSelect.value;
  const ats = pct(hasTailored(a) ? a.tailored_ats_score : a.ats_score);
  return `
    <button type="button" data-app="${a.id}"
      class="w-full rounded-2xl p-4 text-left shadow-xs transition-all ${selected
        ? "border-l-4 border-primary bg-surface-container-lowest shadow-md"
        : "bg-surface-container-lowest hover:bg-surface-container-low"}">
      <div class="flex items-start justify-between gap-2">
        <div class="flex min-w-0 items-center gap-3">
          ${companyAvatar(a.company, "sa-avatar h-9 w-9 text-label-lg")}
          <div class="min-w-0">
            <span class="block truncate text-label-lg text-on-surface">${escapeHtml(a.company || "Unknown company")}</span>
            <span class="font-mono text-[11px] text-on-surface-variant">${escapeHtml(a.source || "")}</span>
          </div>
        </div>
        ${ats !== null ? `<span class="sa-chip-teal whitespace-nowrap">${ats}% ATS</span>` : ""}
      </div>
      <p class="mt-3 truncate text-headline-sm font-semibold text-on-surface">${escapeHtml(a.title || "Untitled role")}</p>
      <div class="mt-3 flex items-center justify-between">
        <span class="sa-chip-primary"><span class="ms text-[14px]">lock_clock</span>Pending human confirmation</span>
        <span class="font-mono text-[11px] text-on-surface-variant">${timeAgo(a.date_created)}</span>
      </div>
    </button>`;
}

function emailCard(e) {
  const failed = e.status === "failed";
  const chip = failed
    ? `<span class="sa-chip-error"><span class="ms text-[14px]">error</span>Failed</span>`
    : e.dry_run
      ? `<span class="sa-chip-amber"><span class="ms text-[14px]">science</span>Dry run — logged only</span>`
      : `<span class="sa-chip-teal"><span class="ms text-[14px]">done_all</span>Sent</span>`;
  return `
    <div class="rounded-2xl bg-surface-container-lowest p-4 shadow-xs">
      <div class="flex items-start justify-between gap-2">
        <div class="flex min-w-0 items-center gap-3">
          ${companyAvatar(e.company, "sa-avatar h-9 w-9 text-label-lg")}
          <div class="min-w-0">
            <span class="block truncate text-label-lg text-on-surface">${escapeHtml(e.company || "—")}</span>
            <span class="font-mono text-[11px] text-on-surface-variant">${escapeHtml(e.to_email)}</span>
          </div>
        </div>
      </div>
      <p class="mt-3 truncate text-headline-sm font-semibold text-on-surface">${escapeHtml(e.subject)}</p>
      ${e.job_title ? `<p class="text-body-sm text-on-surface-variant">${escapeHtml(e.job_title)}</p>` : ""}
      ${failed && e.error_message ? `<p class="mt-2 text-body-sm text-error">${escapeHtml(e.error_message)}</p>` : ""}
      <div class="mt-3 flex items-center justify-between">${chip}<span class="font-mono text-[11px] text-on-surface-variant">${e.sent_at ? new Date(e.sent_at).toLocaleString() : "—"}</span></div>
    </div>`;
}

function renderQueue() {
  const el = document.getElementById("queue");
  const sent = emails.filter((e) => e.status !== "failed");
  const failed = emails.filter((e) => e.status === "failed");
  const counts = { pending: pendingApps.length, sent: sent.length, failed: failed.length };
  document.querySelectorAll("[data-count]").forEach((c) => { c.textContent = `(${counts[c.dataset.count]})`; });

  if (queueTab === "pending") {
    document.getElementById("queue-title").textContent = "Priority queue";
    document.getElementById("queue-sort").textContent = "sort: match desc";
    el.innerHTML = pendingApps.length ? pendingApps.map(pendingCard).join("")
      : `<p class="sa-empty">No drafts awaiting review.</p>`;
  } else {
    const list = queueTab === "sent" ? sent : failed;
    document.getElementById("queue-title").textContent = queueTab === "sent" ? "Dispatch history" : "Needs attention";
    document.getElementById("queue-sort").textContent = "sort: newest";
    el.innerHTML = list.length ? list.map(emailCard).join("")
      : `<p class="sa-empty">${queueTab === "sent" ? "No emails sent yet." : "No failed sends."}</p>`;
  }
}

document.getElementById("queue").addEventListener("click", (e) => {
  const card = e.target.closest("[data-app]");
  if (!card) return;
  applicationSelect.value = card.dataset.app;
  prefillFromSelection();
});

bindTabs(document.getElementById("queue-tabs"), (t) => { queueTab = t; renderQueue(); });

async function loadEmails() {
  try {
    emails = await getJSON("/api/emails?limit=100");
    document.getElementById("stat-sent").textContent = emails.filter((e) => e.status === "sent" && !e.dry_run).length;
    document.getElementById("stat-dry").textContent = emails.filter((e) => e.dry_run && e.status !== "failed").length;
    document.getElementById("stat-failed").textContent = emails.filter((e) => e.status === "failed").length;
  } catch (e) {
    emails = [];
  }
  renderQueue();
}

// ---- send ------------------------------------------------------------------------

document.getElementById("send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const applicationId = applicationSelect.value;
  if (!applicationId) return;

  sendBtn.disabled = true;
  sendMessage.textContent = "Sending…";
  sendMessage.className = "save-message mr-auto";

  try {
    const result = await postJSON(`/api/applications/${applicationId}/send-email`, {
      to_email: document.getElementById("to-email").value,
      subject: document.getElementById("subject").value,
      body: document.getElementById("body").value,
    });
    sendMessage.textContent = result.status === "dry_run" ? "Logged (dry run — not actually sent)." : "Sent.";
    sendMessage.className = "save-message save-success mr-auto";
    document.getElementById("to-email").value = "";
    await Promise.all([loadApplicationOptions(), loadEmails()]);
  } catch (err) {
    sendMessage.textContent = err.message || "Failed to send.";
    sendMessage.className = "save-message save-error mr-auto";
  } finally {
    sendBtn.disabled = !applicationSelect.value;
  }
});

loadGmailStatus();
loadApplicationOptions();
loadEmails();
