function reportBadge(type) {
  return `<span class="status-tag status-${type === "daily" ? "pending_review" : "auto_submitted"}">${type}</span>`;
}

async function loadReports() {
  const el = document.getElementById("reports-list");
  try {
    const reports = await getJSON("/api/reports?limit=50");
    if (!reports.length) {
      el.innerHTML = `<p class="empty">No reports sent yet — the daily and weekly triggers haven't run, or the scheduler isn't running.</p>`;
      return;
    }
    el.innerHTML = reports.map((r) => `
      <div class="report-item">
        <div class="report-item-header">
          ${reportBadge(r.report_type)}
          <span class="status-tag status-${r.status}">${r.status}${r.dry_run ? " (dry run)" : ""}</span>
          <span class="card-sub">${r.sent_at ? new Date(r.sent_at).toLocaleString() : "—"}</span>
        </div>
        ${r.error_message ? `<p class="save-error">${escapeHtml(r.error_message)}</p>` : ""}
        <pre class="cv-preview">${escapeHtml(r.content)}</pre>
      </div>
    `).join("");
  } catch (e) {
    el.innerHTML = `<p class="empty">Could not reach the backend API.</p>`;
  }
}

loadReports();
