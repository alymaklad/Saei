// Dashboard page only. Shared helpers (API_BASE, getJSON, escapeHtml, the
// nav status pill) live in config.js, loaded before this file.

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function scoreBar(score) {
  if (score === null || score === undefined) {
    return `<span class="empty">—</span>`;
  }
  const pct = Math.round(score * 100);
  return `
    <div class="score-bar-wrap">
      <div class="score-bar-track"><div class="score-bar-fill" style="width:${pct}%"></div></div>
      <span>${pct}%</span>
    </div>`;
}

function statusTag(status) {
  const label = (status || "unknown").replace(/_/g, " ");
  return `<span class="status-tag status-${status}">${label}</span>`;
}

async function loadFooterStatus() {
  try {
    const status = await getJSON("/api/status");
    document.getElementById("whitelist-value").textContent =
      status.whitelisted_sources.length ? status.whitelisted_sources.join(", ") : "none (draft-only)";
    document.getElementById("llm-value").textContent = status.llm_provider;
  } catch (e) {
    // footer just stays at "—"
  }
}

async function loadStats() {
  try {
    const stats = await getJSON("/api/stats");
    document.getElementById("stat-jobs").textContent = stats.total_jobs;
    document.getElementById("stat-apps").textContent = stats.total_applications;
    document.getElementById("stat-pending").textContent = stats.pending_review;
    document.getElementById("stat-auto").textContent = stats.auto_submitted;
    document.getElementById("stat-emails").textContent = stats.emails_sent;
  } catch (e) {
    // stat cards just stay at "—"
  }
}

async function loadApplications() {
  const body = document.getElementById("applications-body");
  const count = document.getElementById("applications-count");
  try {
    const apps = await getJSON("/api/applications?limit=50");
    count.textContent = `${apps.length} shown`;

    if (!apps.length) {
      body.innerHTML = `<tr><td colspan="6" class="empty">No applications yet.</td></tr>`;
      return;
    }

    body.innerHTML = apps.map((a) => `
      <tr>
        <td><a href="${a.url}" target="_blank" rel="noopener">${escapeHtml(a.title || "Untitled role")}</a></td>
        <td>${escapeHtml(a.company || "—")}</td>
        <td>${escapeHtml(a.source || "—")}</td>
        <td>${scoreBar(a.ats_score)}</td>
        <td>${statusTag(a.status)}</td>
        <td>${fmtDate(a.date_created)}</td>
      </tr>
    `).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="empty">Could not reach the backend API.</td></tr>`;
    count.textContent = "";
  }
}

async function loadSkillGaps() {
  const list = document.getElementById("skill-gaps-list");
  try {
    const gaps = await getJSON("/api/skill-gaps?limit=10");
    if (!gaps.length) {
      list.innerHTML = `<li class="empty">No recurring gaps yet.</li>`;
      return;
    }
    list.innerHTML = gaps.map((g) => `
      <li><span>${escapeHtml(g.skill)}</span><span class="skill-count">${g.count}</span></li>
    `).join("");
  } catch (e) {
    list.innerHTML = `<li class="empty">Could not reach the backend API.</li>`;
  }
}

async function loadNews() {
  const el = document.getElementById("news-content");
  try {
    const digests = await getJSON("/api/news?limit=1");
    if (!digests.length) {
      el.textContent = "No digest yet — runs weekly.";
      return;
    }
    el.textContent = digests[0].content;
  } catch (e) {
    el.textContent = "Could not reach the backend API.";
  }
}

loadFooterStatus();
loadStats();
loadApplications();
loadSkillGaps();
loadNews();
