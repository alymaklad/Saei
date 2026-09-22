// Dashboard page only. Shared helpers live in config.js (API access,
// escapeHtml, the status pill) and ui.js (chips, avatars, dates, sorting).

// ---- greeting ---------------------------------------------------------------

function paintGreeting(user) {
  const h = new Date().getHours();
  const part = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  const first = user && user.name ? user.name.trim().split(/\s+/)[0] : "";
  document.getElementById("greeting").textContent = first ? `${part}, ${first}` : part;
}
paintGreeting(window.saeiUser);
document.addEventListener("saei:user", (e) => paintGreeting(e.detail));

// ---- "Why?" trigger: opens the shared modal (why-modal.js) --------------
// Every application has ats_score/ats_breakdown (score_node runs for every
// job, regardless of which path it takes afterward); tailored_* fields are
// only set for jobs whose fit score was low enough to trigger a CV rewrite
// -- the modal handles that case gracefully on its own (see renderDocPane
// in why-modal.js), so no branching is needed here.

let applicationsById = {};

function openWhyModalForApplication(id) {
  const a = applicationsById[id];
  if (!a) return;
  openWhyModal({
    // Which engine produced this row's breakdown -- the modal picks its
    // renderer from this, so a legacy row stored before the cutover still
    // renders against legacy pillar keys.
    scoring_engine: a.scoring_engine,
    ats_score: a.ats_score,
    ats_breakdown: a.ats_breakdown,
    tailored_ats_score: a.tailored_ats_score,
    tailored_ats_breakdown: a.tailored_ats_breakdown,
    tailored_download_url: cvDownloadUrl(a.cv_path),
  });
}
window.openWhyModalForApplication = openWhyModalForApplication;

// ---- stats --------------------------------------------------------------------

let stats = null;
let apps = [];
let gaps = [];

async function loadStats() {
  try {
    stats = await getJSON("/api/stats");
    document.getElementById("stat-jobs").textContent = stats.total_jobs;
    document.getElementById("stat-apps").textContent = stats.total_applications;
    document.getElementById("stat-pending").textContent = stats.pending_review;
    document.getElementById("stat-auto").textContent = stats.auto_submitted;
    document.getElementById("stat-emails").textContent = stats.emails_sent;
  } catch (e) {
    // stat cards just stay at "—"
  }
}

function average(values) {
  const v = values.filter((x) => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

function paintAverages() {
  // Averages are over the recent window loaded below (see loadApplications),
  // and the sub-labels say so rather than implying an all-time figure.
  const avgMatch = average(apps.map((a) => a.match_score));
  const bestAts = average(apps.map((a) => (hasTailored(a) ? a.tailored_ats_score : a.ats_score)));
  const baseAts = average(apps.map((a) => a.ats_score));

  const m = pct(avgMatch);
  document.getElementById("stat-avg-match").textContent = m === null ? "—" : `${m}%`;
  document.getElementById("stat-avg-match-bar").style.width = `${m || 0}%`;
  const scored = apps.filter((a) => a.match_score !== null && a.match_score !== undefined).length;
  document.getElementById("stat-avg-match-sub").textContent = scored ? `across ${scored} ranked` : "";

  const b = pct(bestAts);
  document.getElementById("stat-avg-ats").textContent = b === null ? "—" : `${b}%`;
  document.getElementById("stat-avg-ats-bar").style.width = `${b || 0}%`;
  const lift = b !== null && baseAts !== null ? b - pct(baseAts) : 0;
  document.getElementById("stat-avg-ats-sub").textContent = lift > 0 ? `+${lift}% tailored` : "";
}

// ---- caravan route --------------------------------------------------------

function paintCaravan() {
  if (!stats) return;
  const scored = apps.filter((a) => a.ats_score !== null && a.ats_score !== undefined).length;
  const tailored = apps.filter(hasTailored).length;
  const submitted = stats.auto_submitted + stats.emails_sent;

  const steps = [
    { icon: "radar", label: "Search", sub: `${stats.total_jobs} found`, done: stats.total_jobs > 0 },
    { icon: "hub", label: "Match", sub: `${stats.total_applications} ranked`, done: stats.total_applications > 0 },
    { icon: "fact_check", label: "Score & Audit", sub: `${scored} scored`, done: scored > 0 },
    { icon: "edit_document", label: "Tailor CV", sub: `${tailored} tailored`, done: tailored > 0 },
    { icon: "rocket_launch", label: "Apply", sub: stats.pending_review ? `${stats.pending_review} to review` : `${submitted} submitted`, done: submitted > 0 },
    { icon: "trending_up", label: "Grow", sub: gaps.length ? `${gaps.length} skill gaps` : "Roadmap", done: gaps.length > 0 },
  ];

  // The marker sits where the caravan is waiting on you: drafts to review
  // first, otherwise the first stage that hasn't produced anything yet.
  let here = stats.pending_review ? 4 : steps.findIndex((s) => !s.done);
  if (here === -1) here = steps.length - 1;

  document.getElementById("caravan-steps").innerHTML = steps.map((s, i) => {
    if (i === here) {
      return `
        <div class="relative flex flex-col items-center text-center">
          <div class="absolute -top-10 flex animate-bounce flex-col items-center">
            <div class="flex items-center gap-1.5 rounded-full bg-primary px-2.5 py-1 text-label-sm font-bold text-on-primary shadow-md">
              <span class="ms text-[16px]">smart_toy</span>Sa'ei Here
            </div>
            <div class="h-0 w-0 border-l-4 border-r-4 border-t-4 border-l-transparent border-r-transparent border-t-primary"></div>
          </div>
          <div class="-mt-1 mb-2 flex h-12 w-12 items-center justify-center rounded-full bg-surface-container-lowest text-primary shadow-lg ring-4 ring-primary">
            <span class="ms text-[24px]">${s.icon}</span>
          </div>
          <span class="text-label-md font-bold text-primary">${s.label}</span>
          <span class="rounded-full bg-surface-container-lowest px-2 py-0.5 text-label-sm font-semibold text-on-surface shadow-sm">${escapeHtml(s.sub)}</span>
        </div>`;
    }
    const reached = i < here;
    return `
      <div class="flex flex-col items-center text-center">
        <div class="mb-2 flex h-10 w-10 items-center justify-center rounded-full ring-4 ring-surface-container-low ${reached ? "bg-primary text-on-primary shadow-md" : "bg-surface-container-highest text-on-surface-variant"}">
          <span class="ms">${s.icon}</span>
        </div>
        <span class="text-label-md font-bold text-on-surface">${s.label}</span>
        <span class="text-label-sm ${reached ? "font-semibold text-primary" : "text-on-surface-variant"}">${escapeHtml(s.sub)}</span>
      </div>`;
  }).join("");

  // Track fill runs from the first node to the marker node.
  document.getElementById("caravan-fill").style.width = `calc((100% - 4rem) * ${here / (steps.length - 1)})`;
}

// ---- opportunities --------------------------------------------------------

const PREVIEW_COUNT = 5;
let oppFilter = "all";
let oppSort = "match_score";

function opportunityCard(a) {
  const tailoredUrl = hasTailored(a) ? cvDownloadUrl(a.cv_path) : null;
  const p = pct(a.match_score);
  const why = a.match_breakdown || a.ats_breakdown
    ? `<button type="button" class="sa-btn sa-btn-sm bg-surface-container-lowest" data-audit-toggle="${a.id}">
         <span class="ms text-[16px]">visibility</span>Why ${p !== null ? p + "%" : ""}?
       </button>` : "";
  // Inline "match intelligence audit" panel (hidden until Why? is clicked):
  // the ranking breakdown, plus a door into the full ATS breakdown modal.
  const audit = `
    <div class="mt-4 hidden rounded-xl bg-surface-container-lowest p-4 shadow-inner" id="audit-${a.id}">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-2">
          <span class="ms text-secondary">psychology</span>
          <span class="text-label-md font-bold text-on-surface">Sa'ei Match Audit</span>
        </div>
        ${p !== null ? `<span class="text-label-sm font-bold text-secondary">Overall match ${p}%</span>` : ""}
      </div>
      <div class="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">${matchBreakdownTiles(a.match_breakdown)}</div>
      ${a.ats_breakdown ? `
      <div class="flex items-center justify-between pt-1">
        <span class="text-body-sm text-on-surface-variant"><span class="font-semibold text-primary">ATS:</span>
          requirement-by-requirement evidence against your CV${hasTailored(a) ? " and the tailored copy" : ""}.</span>
        <button type="button" class="sa-btn-ghost text-secondary" onclick="openWhyModalForApplication(${a.id})">Full ATS breakdown &rarr;</button>
      </div>` : ""}
    </div>`;
  let primary;
  if (a.status === "pending_review") {
    primary = `<a class="sa-btn-primary sa-btn-sm" href="email.html?application=${a.id}">Review &amp; send</a>`;
  } else if (tailoredUrl) {
    primary = `<a class="sa-btn-primary sa-btn-sm" href="${API_BASE}${tailoredUrl}" target="_blank" rel="noopener">Tailored CV</a>`;
  } else {
    primary = `<a class="sa-btn-primary sa-btn-sm" href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener">Open posting</a>`;
  }
  return `
    <div class="group rounded-2xl bg-surface-container-low p-4 shadow-xs transition-all hover:bg-surface-container">
      <div class="flex flex-col justify-between gap-4 md:flex-row md:items-center">
        <div class="flex min-w-0 items-start gap-3.5">
          ${companyAvatar(a.company)}
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <a href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener"
                 class="text-headline-sm font-bold text-on-surface transition-colors group-hover:text-primary">${escapeHtml(a.title || "Untitled role")}</a>
              ${matchChip(a.match_score)}
              ${atsChip(a.ats_score, a.tailored_ats_score)}
            </div>
            <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-body-sm text-on-surface-variant">
              <span class="font-semibold text-on-surface">${escapeHtml(a.company || "Unknown company")}</span>
              <span>•</span>
              <span class="flex items-center gap-0.5"><span class="ms text-[14px]">travel_explore</span>${escapeHtml(a.source || "—")}</span>
              <span>•</span>
              <span>${timeAgo(a.date_created)}</span>
              ${statusChip(a.status)}
            </div>
          </div>
        </div>
        <div class="flex flex-shrink-0 items-center gap-2 self-end md:self-center">${why}${primary}</div>
      </div>
      ${audit}
    </div>`;
}

document.getElementById("applications-body").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-audit-toggle]");
  if (!btn) return;
  document.getElementById(`audit-${btn.dataset.auditToggle}`).classList.toggle("hidden");
});

function filteredApps() {
  if (oppFilter === "top") return apps.filter((a) => (a.match_score || 0) >= 0.75);
  if (oppFilter === "tailored") return apps.filter(hasTailored);
  if (oppFilter === "applied") return apps.filter(isApplied);
  return apps;
}

function renderOpportunities() {
  const body = document.getElementById("applications-body");
  const list = sortApplicationsBy(filteredApps(), oppSort);
  document.getElementById("opps-count").textContent = `${apps.length} recent`;
  if (!list.length) {
    body.innerHTML = `<p class="sa-empty">${apps.length ? "Nothing in this view yet." : "No applications yet — run a search to find your first roles."}</p>`;
    document.getElementById("opps-footer").textContent = "";
    return;
  }
  body.innerHTML = list.slice(0, PREVIEW_COUNT).map(opportunityCard).join("");
  // Like the Stitch screen: the top card opens with its audit expanded.
  const first = body.querySelector("[id^='audit-']");
  if (first) first.classList.remove("hidden");
  document.getElementById("opps-footer").textContent =
    `Showing ${Math.min(PREVIEW_COUNT, list.length)} of ${list.length}`;
}

bindTabs(document.getElementById("opp-tabs"), (f) => { oppFilter = f; renderOpportunities(); });
document.getElementById("opp-sort").addEventListener("change", (e) => { oppSort = e.target.value; renderOpportunities(); });

// ---- review queue ---------------------------------------------------------

function renderReviewQueue() {
  const el = document.getElementById("review-queue");
  const pending = sortApplicationsBy(apps.filter((a) => a.status === "pending_review"), "match_score");
  if (!pending.length) {
    el.innerHTML = `<p class="sa-empty md:col-span-2">Nothing waiting on you right now.</p>`;
    return;
  }
  el.innerHTML = pending.slice(0, 4).map((a) => {
    const tailored = hasTailored(a);
    const ats = pct(tailored ? a.tailored_ats_score : a.ats_score);
    return `
      <div class="flex flex-col justify-between rounded-xl bg-surface-container-lowest p-4 shadow-xs">
        <div>
          <div class="mb-2 flex items-start justify-between gap-2">
            <span class="text-label-md font-bold text-on-surface">${escapeHtml(a.company || "Unknown")} • ${escapeHtml(a.title || "Untitled role")}</span>
            ${ats !== null ? `<span class="whitespace-nowrap text-label-sm font-bold text-secondary">ATS ${ats}%</span>` : ""}
          </div>
          <p class="text-body-sm text-on-surface-variant">
            ${tailored
              ? `A tailored CV was generated for this role (ATS ${pct(a.ats_score)}% → ${pct(a.tailored_ats_score)}%).`
              : "Drafted with your master CV — it already cleared the fit threshold."}
          </p>
        </div>
        <div class="mt-4 flex items-center justify-between pt-3">
          <span class="text-label-sm text-on-surface-variant">Drafted ${timeAgo(a.date_created)}</span>
          <a href="email.html?application=${a.id}" class="sa-btn-primary sa-btn-sm">Review &amp; send</a>
        </div>
      </div>`;
  }).join("");
  if (pending.length > 4) {
    el.insertAdjacentHTML("beforeend",
      `<a href="applications.html?status=pending_review" class="text-label-md font-bold text-primary hover:underline md:col-span-2">+ ${pending.length - 4} more awaiting review &rarr;</a>`);
  }
}

// ---- data loads -----------------------------------------------------------

async function loadApplications() {
  const body = document.getElementById("applications-body");
  try {
    // The recent window every derived number on this page is computed over.
    apps = await getJSON("/api/applications?limit=200");
    applicationsById = {};
    apps.forEach((a) => { applicationsById[a.id] = a; });
    renderOpportunities();
    renderReviewQueue();
    paintAverages();
  } catch (e) {
    body.innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
    document.getElementById("review-queue").innerHTML = `<p class="sa-empty md:col-span-2">Could not reach the backend API.</p>`;
  }
}

async function loadSkillBoost() {
  const el = document.getElementById("skill-boost");
  try {
    gaps = await getJSON("/api/skill-gaps?limit=10");
    document.getElementById("gaps-chip").textContent = gaps.length ? `${gaps.length} gaps` : "";
    if (!gaps.length) {
      el.innerHTML = `<p class="sa-empty">No recurring gaps yet.</p>`;
      return;
    }
    const max = gaps[0].count;
    el.innerHTML = gaps.slice(0, 4).map((g) => `
      <div class="flex flex-col space-y-1.5">
        <div class="flex items-baseline justify-between text-label-md">
          <span class="font-bold text-on-surface">${escapeHtml(g.skill)}</span>
          <span class="font-semibold text-secondary">${g.count} job${g.count === 1 ? "" : "s"}</span>
        </div>
        <div class="sa-progress h-2"><div class="bg-secondary" style="width:${Math.round((g.count / max) * 100)}%"></div></div>
      </div>`).join("");
  } catch (e) {
    el.innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
  }
}

async function loadFooterStatus() {
  try {
    const status = await getJSON("/api/status");
    document.getElementById("whitelist-value").textContent =
      status.whitelisted_sources.length ? status.whitelisted_sources.join(", ") : "none (draft-only)";
    document.getElementById("llm-value").textContent = status.llm_provider;
    document.getElementById("review-mode-chip").textContent = status.dry_run ? "Dry run — nothing is sent" : "Human-in-the-loop";
  } catch (e) {
    // stays at "—"
  }
}

async function loadActivity() {
  const el = document.getElementById("activity-log");
  const [appsRes, emailsRes, reportsRes] = await Promise.allSettled([
    getJSON("/api/applications?limit=5"),
    getJSON("/api/emails?limit=5"),
    getJSON("/api/reports?limit=5"),
  ]);
  const events = [];
  if (appsRes.status === "fulfilled") {
    appsRes.value.forEach((a) => events.push({
      at: a.date_created, icon: "travel_explore", tone: "text-secondary", who: "Search Agent",
      text: `Found ${a.title || "a role"} at ${a.company || "an unknown company"}${a.ats_score != null ? ` — ATS ${pct(a.ats_score)}%` : ""}.`,
    }));
  }
  if (emailsRes.status === "fulfilled") {
    emailsRes.value.forEach((e) => events.push({
      at: e.sent_at, icon: "outgoing_mail", tone: "text-primary", who: "Email Agent",
      text: `${e.status === "failed" ? "Failed to send" : e.dry_run ? "Logged (dry run)" : "Sent"} "${e.subject}" to ${e.to_email}.`,
    }));
  }
  if (reportsRes.status === "fulfilled") {
    reportsRes.value.forEach((r) => events.push({
      at: r.sent_at, icon: "assignment_turned_in", tone: "text-tertiary", who: "Reports Agent",
      text: `${r.report_type === "daily" ? "Daily summary" : "Weekly digest"} ${r.status}${r.dry_run ? " (dry run)" : ""}.`,
    }));
  }
  if (!events.length) {
    el.innerHTML = `<p class="sa-empty">${appsRes.status === "rejected" ? "Could not reach the backend API." : "No agent activity yet."}</p>`;
    return;
  }
  events.sort((a, b) => new Date(b.at || 0) - new Date(a.at || 0));
  el.innerHTML = events.slice(0, 6).map((ev) => `
    <div class="flex items-start gap-2.5">
      <span class="ms mt-0.5 text-[16px] ${ev.tone}">${ev.icon}</span>
      <div class="min-w-0 flex-1">
        <span class="text-label-sm font-bold text-on-surface">${ev.who}:</span>
        <p class="text-label-sm text-on-surface-variant">${escapeHtml(ev.text)}</p>
        <span class="text-[11px] text-outline">${timeAgo(ev.at)}</span>
      </div>
    </div>`).join("");
}

async function refreshDashboard() {
  await Promise.all([loadStats(), loadApplications(), loadSkillBoost()]);
  paintCaravan();
}

loadFooterStatus();
refreshDashboard();
loadActivity();
