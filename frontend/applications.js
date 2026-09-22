// Applications page: the full pipeline list with filters, and an audit panel
// for the selected application (match breakdown + ATS requirement evidence).

const PAGE_SIZE = 8;

let allApps = [];
let byId = {};
let statusFilter = "all";
let selectedId = null;
let page = 0;

// ---- "Why?" modal adapter (same shape as the Dashboard's) -----------------
function openWhyModalForApplication(id) {
  const a = byId[id];
  if (!a) return;
  openWhyModal({
    scoring_engine: a.scoring_engine,
    ats_score: a.ats_score,
    ats_breakdown: a.ats_breakdown,
    tailored_ats_score: a.tailored_ats_score,
    tailored_ats_breakdown: a.tailored_ats_breakdown,
    tailored_download_url: cvDownloadUrl(a.cv_path),
  });
}
window.openWhyModalForApplication = openWhyModalForApplication;

// ---- filtering ------------------------------------------------------------

function matchesStatus(a, f) {
  if (f === "pending_review") return a.status === "pending_review";
  if (f === "tailored") return hasTailored(a);
  if (f === "applied") return isApplied(a);
  return true;
}

function visibleApps() {
  const q = document.getElementById("app-filter").value.trim().toLowerCase();
  const minMatch = parseFloat(document.getElementById("score-filter").value) || 0;
  const source = document.getElementById("source-filter").value;
  const sortKey = document.getElementById("sort-select").value;
  const list = allApps.filter((a) =>
    matchesStatus(a, statusFilter)
    && (!q || (a.title || "").toLowerCase().includes(q) || (a.company || "").toLowerCase().includes(q))
    && (!minMatch || (a.match_score || 0) >= minMatch)
    && (!source || a.source === source));
  return sortApplicationsBy(list, sortKey);
}

// ---- stream list ------------------------------------------------------------

function streamRow(a) {
  const selected = a.id === selectedId;
  const tailoredUrl = hasTailored(a) ? cvDownloadUrl(a.cv_path) : null;
  return `
    <button type="button" data-id="${a.id}"
      class="flex w-full items-start gap-4 border-l-4 px-6 py-5 text-left transition-colors ${selected
        ? "border-primary bg-surface-container-lowest"
        : "border-transparent hover:bg-surface-container"}">
      ${companyAvatar(a.company, "sa-avatar h-12 w-12")}
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex min-w-0 flex-wrap items-center gap-2">
            <span class="text-headline-sm font-bold text-on-surface">${escapeHtml(a.title || "Untitled role")}</span>
            ${matchChip(a.match_score)}
            ${atsChip(a.ats_score, a.tailored_ats_score)}
          </div>
          ${selected ? `<span class="sa-chip-primary">Audit open <span class="ms text-[14px]">arrow_forward</span></span>` : ""}
        </div>
        <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-body-sm text-on-surface-variant">
          <span class="font-semibold text-on-surface">${escapeHtml(a.company || "Unknown company")}</span>
          <span>•</span><span>${escapeHtml(a.source || "—")}</span>
          <span>•</span><span>${timeAgo(a.date_created)}</span>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-2 text-label-sm text-on-surface-variant">
          <span>Stage:</span>${statusChip(a.status)}
          ${tailoredUrl ? `<span class="sa-chip"><span class="ms text-[14px]">description</span>Tailored CV ready</span>` : ""}
        </div>
      </div>
    </button>`;
}

function renderList() {
  const list = visibleApps();
  const el = document.getElementById("app-list");
  const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  page = Math.min(page, pages - 1);

  if (!list.length) {
    el.innerHTML = `<p class="sa-empty px-6">${allApps.length ? "No applications match these filters." : "No applications yet — run a search to find roles."}</p>`;
  } else {
    el.innerHTML = list.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(streamRow).join(
      `<div class="mx-6 h-px bg-surface-container-highest/70"></div>`);
  }

  const sortText = document.getElementById("sort-select").selectedOptions[0].textContent;
  document.getElementById("sort-label").textContent = `Sorted by ${sortText.toLowerCase()}`;
  document.getElementById("page-info").textContent = list.length
    ? `Showing ${page * PAGE_SIZE + 1}–${Math.min((page + 1) * PAGE_SIZE, list.length)} of ${list.length}`
    : "";
  document.getElementById("page-num").textContent = `Page ${page + 1} of ${pages}`;
  document.getElementById("page-prev").disabled = page === 0;
  document.getElementById("page-next").disabled = page >= pages - 1;

  renderVector(list);
}

document.getElementById("app-list").addEventListener("click", (e) => {
  const row = e.target.closest("button[data-id]");
  if (!row) return;
  selectApplication(Number(row.dataset.id));
});

document.getElementById("page-prev").addEventListener("click", () => { page -= 1; renderList(); });
document.getElementById("page-next").addEventListener("click", () => { page += 1; renderList(); });
["app-filter", "score-filter", "source-filter", "sort-select"].forEach((id) => {
  document.getElementById(id).addEventListener(id === "app-filter" ? "input" : "change", () => { page = 0; renderList(); });
});
bindTabs(document.getElementById("status-tabs"), (f) => { statusFilter = f; page = 0; renderList(); });

// ---- alignment vector ---------------------------------------------------------

function avg(values) {
  const v = values.filter((x) => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

function renderVector(list) {
  const m = pct(avg(list.map((a) => a.match_score)));
  const t = pct(avg(list.map((a) => (hasTailored(a) ? a.tailored_ats_score : a.ats_score))));
  const share = list.length ? Math.round((list.filter(hasTailored).length / list.length) * 100) : null;
  const set = (id, v) => {
    document.getElementById(id).textContent = v === null ? "—" : `${v}%`;
    document.getElementById(`${id}-bar`).style.width = `${v || 0}%`;
  };
  set("vec-match", m);
  set("vec-ats", t);
  set("vec-tailored", share);
  document.getElementById("vector-scope").textContent = `${list.length} application${list.length === 1 ? "" : "s"} in view`;
}

// ---- audit panel ------------------------------------------------------------

const RELATION_TONE = {
  none: "sa-chip-error",
  exact: "sa-chip-teal",
  alias: "sa-chip-teal",
  subset: "sa-chip-amber",
  implied: "sa-chip-amber",
  semantic_support: "sa-chip-amber",
};

function requirementEvidence(breakdown) {
  // Every requirement across every bucket, unmet ones first -- they're the
  // actionable rows.
  const items = [];
  Object.values(breakdown || {}).forEach((bucket) => (bucket.items || []).forEach((it) => items.push(it)));
  if (!items.length) return { html: "", met: 0, total: 0 };
  const met = items.filter((it) => it.relation && it.relation !== "none").length;
  items.sort((x, y) => (x.relation === "none" ? 0 : 1) - (y.relation === "none" ? 0 : 1));
  const html = items.slice(0, 8).map((it) => {
    const found = it.relation && it.relation !== "none";
    const label = found ? relationShort(it) + (it.evidence_location === "claimed" ? ", listed only" : "") : "Not found";
    return `
      <div class="rounded-2xl p-3 ${found ? "bg-surface-container-low" : "bg-error-container/40"}">
        <div class="flex items-start justify-between gap-2">
          <div class="flex items-start gap-2">
            <span class="ms mt-0.5 text-[18px] ${found ? "text-secondary" : "text-error"}">${found ? "check_circle" : "warning"}</span>
            <span class="text-label-lg text-on-surface">${escapeHtml(it.name || "")}</span>
          </div>
          <span class="${RELATION_TONE[it.relation || "none"] || "sa-chip"} whitespace-nowrap">${escapeHtml(label)}</span>
        </div>
        ${found && it.evidence
          ? `<p class="ml-7 mt-1 text-body-sm text-on-surface-variant">“${escapeHtml(String(it.evidence).slice(0, 180))}”</p>`
          : `<p class="ml-7 mt-1 text-body-sm text-on-surface-variant">${escapeHtml(it.importance || "")} requirement · ${it.points_earned ?? 0}/${it.points ?? 0} pts</p>`}
      </div>`;
  }).join("");
  return { html, met, total: items.length, more: Math.max(0, items.length - 8) };
}

function legacyKeywords(breakdown) {
  const kw = breakdown && breakdown.keyword_match;
  if (!kw) return "";
  const chips = (list, cls) => (list || []).map((s) => `<span class="${cls}">${escapeHtml(s)}</span>`).join(" ");
  return `
    <p class="text-body-sm text-on-surface-variant">Scored by the retired four-pillar engine — kept readable, not comparable with newer scores.</p>
    <div class="mt-2 flex flex-wrap gap-1.5">${chips(kw.matched_skills, "sa-chip-teal")}${chips(kw.missing_skills, "sa-chip-error")}</div>`;
}

function renderAudit(a) {
  const panel = document.getElementById("audit-panel");
  if (!a) {
    panel.innerHTML = `<p class="sa-empty">Select an application to audit it.</p>`;
    return;
  }
  const tailored = hasTailored(a);
  const bestAts = tailored ? a.tailored_ats_score : a.ats_score;
  const breakdown = tailored ? a.tailored_ats_breakdown : a.ats_breakdown;
  const ev = a.scoring_engine === "requirements" ? requirementEvidence(breakdown) : { html: "", met: 0, total: 0 };
  const tailoredUrl = tailored ? cvDownloadUrl(a.cv_path) : null;

  panel.innerHTML = `
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <span class="flex items-center gap-1 text-label-sm font-bold uppercase tracking-wider text-primary">
          <span class="ms text-[16px]">shield</span>Transparent audit breakdown
        </span>
        <h2 class="mt-1 text-headline-lg font-bold tracking-tight text-on-surface">Explainable ATS &amp; Match Audit</h2>
        <p class="text-body-sm text-on-surface-variant">${escapeHtml(a.title || "Untitled role")} • ${escapeHtml(a.company || "Unknown company")}</p>
      </div>
      ${companyAvatar(a.company, "sa-avatar h-10 w-10 text-label-lg")}
    </div>

    <div class="mt-5 grid grid-cols-2 gap-4 rounded-2xl bg-surface-container-low p-5">
      ${scoreRing(bestAts, "text-primary", "ATS readiness", tailored ? `tailored CV (was ${pct(a.ats_score)}%)` : "your master CV")}
      ${scoreRing(a.match_score, "text-secondary", "Match", "ranking agent")}
    </div>

    ${a.match_breakdown ? `
    <div class="mt-6">
      <h3 class="mb-3 text-headline-sm font-bold text-on-surface">Why this match score</h3>
      <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">${matchBreakdownTiles(a.match_breakdown)}</div>
    </div>` : ""}

    <div class="mt-6">
      <div class="mb-3 flex items-center justify-between">
        <h3 class="text-headline-sm font-bold text-on-surface">Evidence &amp; Requirement Breakdown</h3>
        ${ev.total ? `<span class="text-label-sm font-semibold text-secondary">${ev.met} / ${ev.total} fulfilled</span>` : ""}
      </div>
      <div class="flex flex-col gap-2">
        ${ev.html || legacyKeywords(breakdown) || `<p class="sa-empty">No ATS breakdown recorded for this job.</p>`}
      </div>
      ${breakdown ? `<button type="button" class="sa-btn-ghost mt-2" onclick="openWhyModalForApplication(${a.id})">
        ${ev.more ? `See all ${ev.total} requirements` : "Open the full breakdown"} with your CV side by side &rarr;</button>` : ""}
    </div>

    <div class="mt-6 flex flex-wrap items-center gap-2">
      ${a.status === "pending_review"
        ? `<a href="email.html?application=${a.id}" class="sa-btn-primary"><span class="ms text-[18px]">send</span>Review &amp; send</a>` : ""}
      ${tailoredUrl ? `<a href="${API_BASE}${tailoredUrl}" target="_blank" rel="noopener" class="sa-btn"><span class="ms text-[18px]">download</span>Tailored CV</a>` : ""}
      <a href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener" class="sa-btn"><span class="ms text-[18px]">open_in_new</span>Posting</a>
    </div>`;
}

function selectApplication(id) {
  selectedId = id;
  const a = byId[id];
  const chip = document.getElementById("selection-chip");
  chip.hidden = !a;
  if (a) chip.textContent = `${a.company || "Unknown"} selected`;
  renderAudit(a);
  renderList();
}

// ---- load -----------------------------------------------------------------

async function loadApplications() {
  try {
    allApps = await getJSON("/api/applications?limit=1000");
  } catch (e) {
    document.getElementById("app-list").innerHTML = `<p class="sa-empty px-6">Could not reach the backend API.</p>`;
    return;
  }
  byId = {};
  allApps.forEach((a) => { byId[a.id] = a; });

  document.querySelectorAll("[data-count]").forEach((el) => {
    const n = allApps.filter((a) => matchesStatus(a, el.dataset.count)).length;
    el.textContent = `(${n})`;
  });
  document.getElementById("hdr-pending").textContent = allApps.filter((a) => a.status === "pending_review").length;
  document.getElementById("hdr-tailored").textContent = allApps.filter(hasTailored).length;

  const sources = [...new Set(allApps.map((a) => a.source).filter(Boolean))].sort();
  document.getElementById("source-filter").insertAdjacentHTML("beforeend",
    sources.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join(""));

  // ?status=pending_review deep link (from the Dashboard's review queue).
  const wanted = new URLSearchParams(location.search).get("status");
  if (wanted) {
    const tab = document.querySelector(`#status-tabs [data-filter="${CSS.escape(wanted)}"]`);
    if (tab) tab.click();
  }

  // ?id=123 deep link (from Search's "Review & audit"): open that one, on
  // the page of the list it sits on.
  const wantedId = Number(new URLSearchParams(location.search).get("id"));
  if (wantedId && byId[wantedId]) {
    const idx = visibleApps().findIndex((a) => a.id === wantedId);
    if (idx >= 0) page = Math.floor(idx / PAGE_SIZE);
    selectApplication(wantedId);
    return;
  }
  const first = visibleApps()[0];
  if (first) selectApplication(first.id); else renderList();
}

loadApplications();
