// CV Studio: master CV, upload, tailored vault, profile section breakdown and
// the ATS & tailoring bench. Shared helpers come from config.js / ui.js /
// why-modal.js.

const fileInput = document.getElementById("cv-file-input");
const dropzone = document.getElementById("dropzone");
const dropzoneText = document.getElementById("dropzone-text");
const uploadBtn = document.getElementById("upload-btn");
const uploadMessage = document.getElementById("upload-message");
const DROPZONE_IDLE_TEXT = "Drag & drop a PDF or DOCX, or click to choose one";

function fmtBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

let cvStatus = null;
let profileCounts = null;

function masterStatsHTML() {
  if (!profileCounts) return "";
  const cell = (label, value) => `
    <div>
      <span class="block text-label-sm text-on-surface-variant">${label}</span>
      <span class="text-headline-sm font-bold text-on-surface">${value}</span>
    </div>`;
  return `
    <div class="mt-5 grid grid-cols-3 gap-3 rounded-2xl bg-surface-container-low p-4">
      ${cell("Skills listed", profileCounts.skills)}
      ${cell("Career chapters", profileCounts.experience)}
      ${cell("Projects", profileCounts.projects)}
    </div>`;
}

function paintCvStatus() {
  const el = document.getElementById("cv-current");
  const status = cvStatus;
  if (!status) return;
  if (!status.has_cv) {
    el.innerHTML = `
      <span class="sa-eyebrow">Master identity</span>
      <p class="sa-empty">No CV uploaded yet — use the upload card below.</p>`;
    return;
  }

  const head = `
    <div class="flex items-start gap-4">
      <div class="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-xl bg-primary-fixed text-primary">
        <span class="ms text-[26px]">description</span>
      </div>
      <div class="min-w-0 flex-1">
        <div class="flex items-center justify-between gap-2">
          <span class="text-label-sm font-bold uppercase tracking-wider text-primary">Master identity</span>
          ${status.parse_error
            ? `<span class="sa-chip-error">Unreadable</span>`
            : `<span class="sa-chip-teal"><span class="h-1.5 w-1.5 rounded-full bg-secondary"></span>Parsed</span>`}
        </div>
        <p class="truncate text-headline-sm font-bold text-on-surface" title="${escapeHtml(status.filename)}">${escapeHtml(status.filename)}</p>
        <p class="text-body-sm text-on-surface-variant">${fmtBytes(status.size_bytes)} · updated ${status.modified ? timeAgo(new Date(status.modified * 1000).toISOString()) : "—"}</p>
      </div>
    </div>`;

  if (status.parse_error) {
    el.innerHTML = `${head}<p class="save-error mt-4">Could not read this file: ${escapeHtml(status.parse_error)}</p>`;
    return;
  }

  el.innerHTML = `
    ${head}
    ${masterStatsHTML()}
    <blockquote class="mt-4 max-h-40 overflow-hidden whitespace-pre-line rounded-2xl bg-surface-container-low p-4 text-body-sm text-on-surface-variant [mask-image:linear-gradient(to_bottom,black_70%,transparent)]">${escapeHtml(status.preview)}${status.char_count > 400 ? "…" : ""}</blockquote>
    <div class="mt-4 flex flex-wrap gap-2">
      <a href="${API_BASE}${status.file_url}" target="_blank" rel="noopener" class="sa-btn sa-btn-sm"><span class="ms text-[16px]">visibility</span>Preview master CV</a>
      <a href="${API_BASE}${status.file_url}" download class="sa-btn sa-btn-sm"><span class="ms text-[16px]">download</span>Download</a>
      <a href="profile.html" class="sa-btn sa-btn-sm"><span class="ms text-[16px]">hub</span>Career profile</a>
    </div>`;
}

async function loadCvStatus() {
  try {
    cvStatus = await getJSON("/api/cv/status");
    paintCvStatus();
  } catch (e) {
    document.getElementById("cv-current").innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
  }
}

// ---- profile section breakdown ------------------------------------------------

function sectionTile(icon, title, meta, body) {
  return `
    <div class="rounded-2xl bg-surface-container-low p-3.5">
      <div class="mb-1.5 flex items-center justify-between gap-2">
        <span class="flex items-center gap-1.5 text-label-lg text-on-surface"><span class="ms text-[18px] text-tertiary">${icon}</span>${title}</span>
        <span class="text-label-sm font-semibold text-secondary">${meta}</span>
      </div>
      ${body}
    </div>`;
}

async function loadProfileBreakdown() {
  const el = document.getElementById("section-breakdown");
  try {
    const payload = await getJSON("/api/profile");
    const p = payload.profile || {};
    const exp = p.experience || [], proj = p.projects || [], edu = p.education || [];
    const skills = p.skills_claimed || [], certs = p.certifications || [];
    profileCounts = { skills: skills.length, experience: exp.length, projects: proj.length };
    paintCvStatus();

    if (!payload.has_profile) {
      el.innerHTML = `<p class="sa-empty">No profile yet — upload a CV and it's extracted automatically.</p>`;
      return;
    }
    const list = (items, fmt) => items.length
      ? `<ul class="space-y-1 text-body-sm text-on-surface-variant">${items.slice(0, 4).map((i) => `<li>${fmt(i)}</li>`).join("")}</ul>`
      : `<p class="text-body-sm italic text-on-surface-variant">None yet.</p>`;
    el.innerHTML = [
      sectionTile("work", "Experience", `${exp.length} role${exp.length === 1 ? "" : "s"}`,
        list(exp, (e) => `<strong class="text-on-surface">${escapeHtml(e.title || "Role")}</strong> · ${escapeHtml(e.organization || "")}`)),
      sectionTile("hub", "Technical stack", `${skills.length} listed`,
        skills.length ? `<div class="flex flex-wrap gap-1.5">${skills.slice(0, 12).map((s) => `<span class="rounded-md bg-surface-container-lowest px-2 py-0.5 text-label-sm text-on-surface">${escapeHtml(s)}</span>`).join("")}${skills.length > 12 ? `<span class="text-label-sm text-on-surface-variant">+${skills.length - 12} more</span>` : ""}</div>` : `<p class="text-body-sm italic text-on-surface-variant">None yet.</p>`),
      sectionTile("rocket_launch", "Projects", `${proj.length}`,
        list(proj, (x) => escapeHtml(x.name || "Project"))),
      sectionTile("school", "Education & certifications", `${edu.length + certs.length}`,
        list([...edu.map((e) => `${escapeHtml(e.degree || "")}${e.field ? `, ${escapeHtml(e.field)}` : ""} — ${escapeHtml(e.institution || "")}`),
              ...certs.map((c) => escapeHtml(c.name || c.title || "Certification"))], (x) => x)),
    ].join("");
  } catch (e) {
    el.innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
  }
}

// ---- upload -------------------------------------------------------------------

function setSelectedFile(file) {
  if (!file) return;
  const ok = /\.(pdf|docx)$/i.test(file.name);
  if (!ok) {
    uploadMessage.textContent = "Only .pdf or .docx files are supported.";
    uploadMessage.className = "save-message save-error";
    fileInput.value = "";
    uploadBtn.disabled = true;
    return;
  }
  dropzoneText.textContent = file.name;
  uploadBtn.disabled = false;
  uploadMessage.textContent = "";
  uploadMessage.className = "save-message";
}

fileInput.addEventListener("change", () => setSelectedFile(fileInput.files[0]));

dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dropzone-active");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dropzone-active"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dropzone-active");
  const file = e.dataTransfer.files[0];
  if (file) {
    fileInput.files = e.dataTransfer.files;
    setSelectedFile(file);
  }
});

uploadBtn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  uploadBtn.disabled = true;
  uploadMessage.textContent = "Uploading…";
  uploadMessage.className = "save-message";

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(`${API_BASE}/api/cv/upload`, { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);

    // Uploading a CV is also what fills the Profile page, so say so -- and
    // say it plainly when the extraction didn't run, because the profile is
    // then still describing the previous file.
    if (data.profile_extracted) {
      uploadMessage.innerHTML =
        'Uploaded, and your profile was filled from it. '
        + '<a href="profile.html">Review it</a>.';
      try { sessionStorage.removeItem("saei.sidebarUser"); } catch (e) { /* ignore */ }
    } else if (data.profile_error) {
      uploadMessage.innerHTML =
        'Uploaded, but your profile could not be read from it: '
        + `${escapeHtml(data.profile_error)} `
        + 'Open the <a href="profile.html">Profile page</a> and press '
        + '"Re-extract from CV" to try again.';
    } else {
      uploadMessage.textContent = "Uploaded.";
    }
    uploadMessage.className = "save-message save-success";
    dropzoneText.textContent = DROPZONE_IDLE_TEXT;
    fileInput.value = "";
    await Promise.all([loadCvStatus(), loadProfileBreakdown()]);
  } catch (e) {
    uploadMessage.textContent = e.message || "Upload failed.";
    uploadMessage.className = "save-message save-error";
    uploadBtn.disabled = false;
  }
});

// ---- "Why?" trigger: opens the shared modal (why-modal.js) --------------
// This page's own record shape (from /api/cv/rewrites) is adapted to the
// modal's generic { ats_score, ats_breakdown, tailored_ats_score,
// tailored_ats_breakdown, tailored_download_url } shape here.

let rewritesById = {};

function openWhyModalForRewrite(applicationId) {
  const r = rewritesById[applicationId];
  if (!r) return;
  openWhyModal({
    // Which engine produced this row's breakdown -- the modal picks its
    // renderer from this, so a legacy row stored before the cutover still
    // renders against legacy pillar keys.
    scoring_engine: r.scoring_engine,
    ats_score: r.ats_score,
    ats_breakdown: r.ats_breakdown,
    tailored_ats_score: r.tailored_ats_score,
    tailored_ats_breakdown: r.tailored_ats_breakdown,
    tailored_download_url: r.download_url,
  });
}
window.openWhyModalForRewrite = openWhyModalForRewrite;

// ---- tailored vault -------------------------------------------------------------

const VAULT_PREVIEW = 6;
let allRewrites = [];
let vaultExpanded = false;

function liftChip(r) {
  const o = pct(r.ats_score), t = pct(r.tailored_ats_score);
  if (o === null && t === null) return "";
  if (o === null || t === null) return `<span class="sa-chip">ATS ${t ?? o}%</span>`;
  const d = t - o;
  const cls = d > 0 ? "sa-chip-teal" : d < 0 ? "sa-chip-error" : "sa-chip";
  return `<span class="${cls}">${o}% <span class="ms text-[12px]">arrow_forward</span> ${t}% (${d > 0 ? "+" : ""}${d} lift)</span>`;
}

function vaultCard(r) {
  return `
    <div class="rounded-2xl bg-surface-container-low p-4">
      <div class="flex items-start gap-3">
        ${companyAvatar(r.company, "sa-avatar h-10 w-10 text-label-lg")}
        <div class="min-w-0 flex-1">
          <p class="text-headline-sm font-bold text-on-surface">${escapeHtml(r.company || "Unknown company")}</p>
          <p class="text-body-sm text-on-surface-variant">${escapeHtml(r.job_title || "Untitled role")}</p>
          <div class="mt-2 flex flex-wrap items-center gap-2">
            ${liftChip(r)}
            <span class="text-label-sm text-on-surface-variant">${fmtDate(r.date_created)}</span>
          </div>
          <div class="mt-3 flex flex-wrap items-center gap-2">
            ${r.tailored_ats_explanation || r.tailored_ats_breakdown
              ? `<button type="button" class="sa-btn sa-btn-sm bg-surface-container-lowest" onclick="openWhyModalForRewrite(${r.application_id})"><span class="ms text-[16px]">difference</span>Compare</button>` : ""}
            <a href="${API_BASE}${r.download_url}" target="_blank" rel="noopener" class="sa-btn-primary sa-btn-sm"><span class="ms text-[16px]">download</span>Download</a>
          </div>
        </div>
      </div>
    </div>`;
}

function renderRewritesRows(rewrites) {
  const body = document.getElementById("cv-rewrites-body");
  const more = document.getElementById("cv-rewrites-more");
  if (!rewrites.length) {
    body.innerHTML = `<p class="sa-empty">${allRewrites.length ? "No matches." : "None yet — generated when a job's ATS score is below the fit threshold."}</p>`;
    more.hidden = true;
    return;
  }
  const shown = vaultExpanded ? rewrites : rewrites.slice(0, VAULT_PREVIEW);
  body.innerHTML = shown.map(vaultCard).join("");
  more.hidden = shown.length >= rewrites.length;
  more.textContent = `Show all ${rewrites.length} tailored CVs →`;
}

function applyRewritesFilter() {
  const q = document.getElementById("cv-filter").value.trim().toLowerCase();
  if (!q) return renderRewritesRows(allRewrites);
  renderRewritesRows(allRewrites.filter((r) =>
    (r.job_title || "").toLowerCase().includes(q) || (r.company || "").toLowerCase().includes(q)
  ));
}

document.getElementById("cv-rewrites-more").addEventListener("click", () => {
  vaultExpanded = true;
  applyRewritesFilter();
});

function updateCvHeaderStats(rewrites) {
  document.getElementById("cv-variant-count").textContent = rewrites.length;
  const companies = new Set(rewrites.map((r) => r.company).filter(Boolean)).size;
  document.getElementById("cv-variant-sub").textContent = companies ? `across ${companies} employers` : "Generated across target roles";

  const best = rewrites
    .map((r) => (r.tailored_ats_score !== null && r.tailored_ats_score !== undefined) ? r.tailored_ats_score : r.ats_score)
    .filter((s) => s !== null && s !== undefined);
  document.getElementById("cv-avg-score").textContent = best.length
    ? fmtPct(best.reduce((a, b) => a + b, 0) / best.length) : "—";

  // Lift only over rows that have BOTH numbers -- a missing baseline isn't a
  // zero, and counting it as one would inflate the average.
  const pairs = rewrites.filter((r) => r.ats_score != null && r.tailored_ats_score != null);
  if (!pairs.length) return;
  const o = pairs.reduce((s, r) => s + r.ats_score, 0) / pairs.length;
  const t = pairs.reduce((s, r) => s + r.tailored_ats_score, 0) / pairs.length;
  const d = Math.round((t - o) * 100);
  const liftEl = document.getElementById("cv-avg-lift");
  liftEl.textContent = `${d > 0 ? "+" : ""}${d} pts`;
  liftEl.classList.toggle("text-error", d < 0);
  liftEl.classList.toggle("text-secondary", d >= 0);
  document.getElementById("cv-avg-lift-sub").textContent = `${fmtPct(o)} → ${fmtPct(t)} composite`;
}

async function loadCvRewrites() {
  const body = document.getElementById("cv-rewrites-body");
  try {
    const rewrites = await getJSON("/api/cv/rewrites?limit=50");
    updateCvHeaderStats(rewrites);

    rewritesById = {};
    rewrites.forEach((r) => { rewritesById[r.application_id] = r; });
    allRewrites = rewrites;

    renderRewritesRows(allRewrites);
  } catch (e) {
    body.innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
  }
}

// ---- ATS & tailoring debug bench -------------------------------------------
// Reuses why-modal.js's pillar labels/ordering so a score shown here reads
// exactly like the same score shown in the Why? modal -- two renderings of
// one number that disagreed cosmetically would undermine the whole point.

const debugRunBtn = document.getElementById("debug-run");
const debugMessage = document.getElementById("debug-message");
const debugResults = document.getElementById("debug-results");

// Bucket order for the requirements engine. Kept in sync with
// config.JOB_MATCH_WEIGHTS by hand; a bucket missing from here still renders,
// it just sorts last.
const BUCKET_ORDER = [
  "required_skills", "experience", "preferred_skills",
  "responsibilities", "education",
];

// Two independent axes: how the CV term relates to the requirement, and where
// the evidence sits. The relation label comes from the backend so the two
// never drift apart. "Semantic support" rather than "Equivalent" is
// deliberate -- CNN supports a Deep Learning requirement without being
// another word for it, and a label saying "Equivalent" beside a partial score
// contradicts the score.
const MATCH_CLASS_LABEL = {
  explicit: "Demonstrated",
  claimed: "Listed",
  implied: "Prerequisite",
  subset: "Subset",
  semantic_support: "Semantic support",
  missing: "Not found",
};

function debugPillars(breakdown) {
  // Handles both engines. The requirements engine names its own buckets and
  // ships a `label`; the legacy engine's four pillars come from why-modal.js
  // so a score shown here reads exactly like the same score in the Why? modal.
  const isRequirements = Object.values(breakdown).some((d) => d && d.label);
  const order = isRequirements
    ? BUCKET_ORDER.filter((k) => breakdown[k]).concat(
        Object.keys(breakdown).filter((k) => !BUCKET_ORDER.includes(k)))
    : PILLAR_ORDER.filter((k) => breakdown[k]);

  return order.map((k) => {
    const d = breakdown[k];
    const pct = Math.round((d.score || 0) * 100);
    const name = d.label || PILLAR_LABELS[k] || k;
    // A redistributed weight differs from the configured one, and hiding that
    // would make the arithmetic impossible to follow.
    const reweighted = d.base_weight && Math.abs(d.base_weight - d.weight) > 0.001
      ? ` (base ${Math.round(d.base_weight * 100)}%)` : "";
    const evidence = isRequirements
      ? (d.detail && d.detail.note ? d.detail.note : "")
      : pillarEvidence(k, d);
    return `
      <div class="score-pillar">
        <div class="score-pillar-head">
          <span class="score-pillar-name">${escapeHtml(name)}</span>
          <span class="score-pillar-meta">${pct}% &middot; weight ${Math.round(d.weight * 100)}%${reweighted}</span>
        </div>
        <div class="score-bar-track"><div class="score-bar-fill ${fillClass(d.score)}" style="width:${pct}%"></div></div>
        ${evidence ? `<div class="score-pillar-evidence">${escapeHtml(evidence)}</div>` : ""}
        ${d.items && d.items.length ? requirementRows(d.items) : ""}
      </div>`;
  }).join("");
}

function requirementRows(items) {
  // Every point awarded traces to a line of the CV. This table is the whole
  // argument for the requirements engine: a keyword count can only tell you
  // "9 of 26", never which nine or on what evidence.
  const rows = items.map((it) => {
    const relation = it.relation || (it.match === "missing" ? "none" : "exact");
    const relLabel = it.relation_label || MATCH_CLASS_LABEL[it.match] || it.match;
    // Two chips, because these vary independently: an exact term buried in a
    // skills list is weaker evidence than a subset term inside a dated role,
    // and one combined label can't say that.
    const evidenceChip = it.evidence_location === "claimed"
      ? `<span class="req-match req-loc-claimed">Listed only</span>` : "";
    return `
    <tr class="req-${escapeHtml(relation)}">
      <td>${escapeHtml(it.name)}</td>
      <td><span class="req-tag req-${escapeHtml(it.importance)}">${escapeHtml(it.importance)}</span></td>
      <td>${escapeHtml((it.category || "").replace(/_/g, " "))}</td>
      <td><span class="req-match req-rel-${escapeHtml(relation)}">${escapeHtml(relLabel)}</span>${evidenceChip}</td>
      <td class="req-pts">${it.points_earned}/${it.points}</td>
      <td class="req-evidence">${it.evidence ? escapeHtml(it.evidence) : "&mdash;"}${
        it.via ? `<span class="req-via">${escapeHtml(it.via)}</span>` : ""}</td>
    </tr>`;
  }).join("");
  return `<div class="req-table-wrap"><table class="req-table">
    <thead><tr><th>Requirement</th><th></th><th>Type</th><th>How it matched</th><th>Pts</th><th>Evidence in your CV</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderCompatibility(c) {
  if (!c) return "";
  const key = c.status === "Pass" ? "good" : (c.status === "Warning" ? "mid" : "bad");
  const issues = (c.issues || []).length
    ? `<ul class="compat-issues">${c.issues.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>`
    : `<p class="card-sub">No parsing problems detected.</p>`;
  return `
    <div class="debug-block">
      <div class="debug-headline">
        <span class="compat-status compat-${key}">${escapeHtml(c.status)}</span>
        <span class="debug-score-small">${Math.round(c.score * 100)}%</span>
        <span class="debug-sub">ATS compatibility &mdash; can a parser read this CV? Depends only on the CV, not on any job, so it is reported separately rather than mixed into the match score.</span>
      </div>
      ${issues}
    </div>`;
}

function renderDebugResults(r) {
  const ats = r.ats;
  const rw = r.rewrite;
  const engineName = "Job match (requirement matching)";

  // tailored_only: one number was asked for, so the untailored score is not
  // rendered at all and the tailored block below drops its "up from X" line.
  const tailoredOnly = r.score_mode === "tailored_only";

  let html = renderCompatibility(r.compatibility);

  if (!tailoredOnly) {
    html += `
    <div class="debug-block">
      <div class="debug-headline">
        <span class="debug-score">${Math.round(ats.score * 100)}%</span>
        <span class="rating-badge rating-${ratingKey(ats.score)}">${escapeHtml(ats.rating)}</span>
        <span class="debug-sub">${escapeHtml(engineName)} &middot; untailored &middot; scored from your ${escapeHtml(r.profile_source || "profile")} &middot; fit threshold ${Math.round(r.fit_threshold * 100)}%${
          ats.cv_years != null ? ` &middot; ${ats.cv_years} yrs professional experience from dated entries` : ""}</span>
      </div>
      <div class="score-pillars">${debugPillars(ats.breakdown)}</div>
      ${(ats.inactive_buckets || []).length ? `<p class="card-sub">Not applicable to this posting, weight redistributed: ${
        ats.inactive_buckets.map((b) => escapeHtml(b.replace(/_/g, " "))).join(", ")}.</p>` : ""}
    </div>`;
  }

  if (rw) {
    const delta = rw.delta;
    const arrow = delta >= 0 ? "&uarr;" : "&darr;";
    html += `
      <div class="debug-block">
        <div class="debug-headline">
          <span class="debug-score">${Math.round(rw.tailored_score * 100)}%</span>
          <span class="rating-badge rating-${ratingKey(rw.tailored_score)}">${escapeHtml(rw.tailored_rating)}</span>
          <span class="debug-sub ${delta >= 0 ? "delta-up" : "delta-down"}">
            ${tailoredOnly ? escapeHtml(rw.triggered_by)
                           : `${arrow} ${Math.abs(Math.round(delta * 100))} points after tailoring &middot; ${escapeHtml(rw.triggered_by)}`}
          </span>
        </div>
        <div class="score-pillars">${debugPillars(rw.tailored_breakdown)}</div>
        <div class="debug-why">${escapeHtml(rw.improvement)}</div>
        ${(rw.integrity_warnings || []).length ? `
        <div class="debug-warnings">
          <div class="debug-warnings-title">Checks that rejected something</div>
          <ul>${rw.integrity_warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>
        </div>` : ""}
        ${rw.target_coverage && (rw.target_coverage.used || []).length + (rw.target_coverage.missed || []).length ? `
        <div class="debug-targets">
          <div class="debug-warnings-title">The job's wording</div>
          <p class="card-sub">Terms this CV already earned, rewritten in the words the posting uses.</p>
          ${(rw.target_coverage.used || []).length ? `<p><span class="target-yes">Used</span> ${rw.target_coverage.used.map(escapeHtml).join(", ")}</p>` : ""}
          ${(rw.target_coverage.missed || []).length ? `<p><span class="target-no">Not used</span> ${rw.target_coverage.missed.map(escapeHtml).join(", ")}</p>` : ""}
        </div>` : ""}
        ${(rw.listed_only || []).length ? `
        <div class="debug-targets">
          <div class="debug-warnings-title">Only in your skills list</div>
          <p class="card-sub">These earn partial credit because no role or project on your profile mentions them. If you used one in a specific role, adding it there on the Profile page is worth the difference.</p>
          <p>${rw.listed_only.map(escapeHtml).join(", ")}</p>
        </div>` : ""}
        ${rw.pdf_url ? `<p><a class="btn" href="${API_BASE}${rw.pdf_url}" target="_blank" rel="noopener">Open rendered PDF</a></p>` : ""}
        <div class="debug-sub" style="margin-top:14px">Tailored CV text (${rw.tailored_chars.toLocaleString()} chars)</div>
        <pre class="debug-cv">${escapeHtml(rw.tailored_cv)}</pre>
      </div>`;
  } else if (r.rewrite_skipped_because) {
    html += `<div class="debug-block"><p class="card-sub">Rewrite skipped — ${escapeHtml(r.rewrite_skipped_because)}.</p></div>`;
  }

  const t = r.timings || {};
  html += `<p class="debug-sub">Timings: ${Object.entries(t).map(([k, v]) => `${escapeHtml(k)} ${v}s`).join(" &middot; ")} &middot; LLM: ${escapeHtml(r.llm_provider)}${
    ats.cv_profile_cached ? " &middot; CV profile served from cache" : ""}</p>`;
  if (r.pdf_error) html += `<p class="save-error">PDF render failed: ${escapeHtml(r.pdf_error)}</p>`;

  debugResults.innerHTML = html;
  debugResults.hidden = false;
}

debugRunBtn.addEventListener("click", async () => {
  const jd = document.getElementById("debug-jd").value.trim();
  if (!jd) {
    debugMessage.textContent = "Paste a job description first.";
    debugMessage.className = "save-message save-error";
    return;
  }
  debugRunBtn.disabled = true;
  debugResults.hidden = true;
  // A local model can take a while, and the request blocks -- say so rather
  // than leaving a dead-looking button.
  debugMessage.textContent = "Running… this makes real LLM calls and can take a minute on a local model.";
  debugMessage.className = "save-message";
  try {
    const result = await postJSON("/api/debug/ats", {
      job_description: jd,
      force_rewrite: document.getElementById("debug-force-rewrite").checked,
      render_pdf: document.getElementById("debug-render-pdf").checked,
    });
    debugMessage.textContent = "Done.";
    debugMessage.className = "save-message save-success";
    renderDebugResults(result);
  } catch (err) {
    debugMessage.textContent = err.message || "Run failed.";
    debugMessage.className = "save-message save-error";
  } finally {
    debugRunBtn.disabled = false;
  }
});

document.getElementById("cv-filter").addEventListener("input", applyRewritesFilter);

loadCvStatus();
loadCvRewrites();
loadProfileBreakdown();
