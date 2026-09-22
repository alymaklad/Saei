// Shared "Why?" modal -- renders a job's ATS score breakdown (original CV
// vs. tailored CV, if one was generated) side by side with the actual
// document, matched/missing keyword chips, and per-pillar evidence. Used by
// both the Dashboard's applications table and the CV page's Tailored CVs
// table -- both pages include the same modal markup (#why-modal, see
// index.html / cv.html) and this one script drives it.
//
// Load order matters: config.js (API_BASE/getJSON/escapeHtml) must load
// before this file, and this file must load before app.js/cv.js.
//
// Callers open the modal with a plain "record" object shaped like:
//   { ats_score, ats_breakdown, tailored_ats_score, tailored_ats_breakdown,
//     tailored_download_url }
// -- tailored_* fields may be null/undefined when a job never went through
// the CV-rewrite path (see openWhyModalForApplication in app.js and
// openWhyModalForRewrite in cv.js for how each page builds one of these
// from its own API response shape).

function fmtPct(score) {
  return score !== null && score !== undefined ? Math.round(score * 100) + "%" : "—";
}

const RATING_LABEL = { excellent: "Excellent", needs_work: "Needs Work", critical: "Critical" };

function ratingKey(score) {
  if (score === null || score === undefined) return "critical";
  if (score >= 0.8) return "excellent";
  if (score >= 0.6) return "needs_work";
  return "critical";
}

// Mirrors agents/ats_agent.py's WEIGHTS/PILLAR_LABELS ordering.
const PILLAR_ORDER = ["keyword_match", "formatting", "section_completeness", "experience_alignment"];
const PILLAR_LABELS = {
  keyword_match: "Keyword Match",
  formatting: "Formatting & Parsability",
  section_completeness: "Section Completeness",
  experience_alignment: "Experience Alignment",
};

function fillClass(score) {
  if (score >= 0.8) return "fill-ok";
  if (score >= 0.6) return "";
  return "fill-danger";
}

function pillarEvidence(key, pillar) {
  if (key === "keyword_match") {
    const n = pillar.matched_skills ? pillar.matched_skills.length : 0;
    const total = pillar.required_skills ? pillar.required_skills.length : 0;
    return `Matched ${n} of ${total} required skills/keywords.`;
  }
  if (key === "formatting") {
    return pillar.issues && pillar.issues.length
      ? pillar.issues.join("; ") + "."
      : "No formatting issues detected.";
  }
  if (key === "section_completeness") {
    return pillar.missing_sections && pillar.missing_sections.length
      ? `Missing: ${pillar.missing_sections.join(", ")}.`
      : "All standard sections found.";
  }
  if (key === "experience_alignment") {
    return pillar.note || "";
  }
  return "";
}

// ---- requirements-engine rendering ------------------------------------------
//
// There is one scoring engine now, but not one shape of stored row. The
// four-pillar scorer was retired from the code; the rows it wrote are still in
// the database, and a breakdown is not self-describing enough to sniff
// reliably, so every record carries `scoring_engine` and the renderers
// dispatch on it. Rows written before that column existed come back as
// "legacy" from the API, which is what they are.
//
//   legacy       { keyword_match: {...}, formatting: {...}, ... }   ARCHIVED
//   requirements { required_skills: {label, score, weight, items:[...]}, ... }
//
// The legacy branch below is kept for reading history, not for scoring. It
// says so on screen, because those numbers came from a scorer whose defects
// are the reason it was replaced -- see SCORING.md.

const RELATION_SHORT = {
  exact: "Exact", alias: "Alias", implied: "Prerequisite", subset: "Subset",
  semantic_support: "Semantic support", none: "Not found",
};

// A relation this map has never heard of still has to render as words. The
// backend ships `relation_label` on every row for exactly this reason; adding
// `implied` here without the fallback would just move the next "undefined"
// badge one release down the road.
function relationShort(item) {
  return RELATION_SHORT[item.relation] || item.relation_label || item.relation;
}

function requirementRow(item) {
  const found = item.relation && item.relation !== "none";
  const label = found
    ? relationShort(item) + (item.evidence_location === "claimed" ? ", listed only" : "")
    : "Not found";
  const points = `${item.points_earned ?? 0}/${item.points ?? 0} pts`;
  const evidence = found && item.evidence
    ? `<p class="req-evidence">“${escapeHtml(String(item.evidence).slice(0, 160))}”</p>`
    : "";
  const via = item.via ? `<p class="req-via">${escapeHtml(item.via)}</p>` : "";
  return `
    <li class="req-row${found ? "" : " req-row-missing"}">
      <span class="req-name">${escapeHtml(item.name || "")}</span>
      <span class="req-tags">
        <span class="req-chip req-rel-${item.relation || "none"}">${escapeHtml(label)}</span>
        <span class="req-points">${points}</span>
      </span>
      ${evidence}${via}
    </li>
  `;
}

function renderRequirementBuckets(breakdown) {
  if (!breakdown || !Object.keys(breakdown).length) {
    return `<p class="empty">No score breakdown recorded for this CV.</p>`;
  }
  return Object.entries(breakdown).map(([, bucket]) => {
    const pct = Math.round((bucket.score || 0) * 100);
    // The experience bucket carries a prose note instead of requirement rows;
    // it is scored from merged CV date intervals, not from term matching.
    const note = bucket.detail && bucket.detail.note
      ? `<p class="score-pillar-evidence">${escapeHtml(bucket.detail.note)}</p>` : "";
    const items = (bucket.items || []).length
      ? `<ul class="req-list">${bucket.items.map(requirementRow).join("")}</ul>` : "";
    return `
      <div class="score-pillar-row">
        <div class="score-pillar-head">
          <span class="score-pillar-name">${escapeHtml(bucket.label || "")}</span>
          <span class="score-pillar-meta">${pct}% &middot; weight ${Math.round((bucket.weight || 0) * 100)}%</span>
        </div>
        <div class="score-pillar-track"><div class="score-pillar-fill ${fillClass(bucket.score || 0)}" style="width:${pct}%"></div></div>
        ${note}${items}
      </div>
    `;
  }).join("");
}

function requirementChips(breakdown) {
  const matched = [], missing = [];
  Object.values(breakdown || {}).forEach((bucket) => {
    (bucket.items || []).forEach((item) => {
      (item.relation && item.relation !== "none" ? matched : missing).push(item.name);
    });
  });
  return { matched, missing };
}

/** Every CV surface form that actually matched — what to highlight in the
 *  document pane. The requirement's own name is often NOT what appears in the
 *  CV (a subset match highlights "CNN", not "Deep Learning"). */
function requirementMatchedForms(breakdown) {
  const forms = [];
  Object.values(breakdown || {}).forEach((bucket) => {
    (bucket.items || []).forEach((item) => {
      if (item.matched_form) forms.push(item.matched_form);
    });
  });
  return forms;
}

function isRequirementsRecord(record) {
  return (record && record.scoring_engine) === "requirements";
}

function renderPillars(breakdown) {
  if (!breakdown) return `<p class="empty">No score breakdown recorded for this CV.</p>`;
  if (isRequirementsRecord(activeRecord)) return renderRequirementBuckets(breakdown);
  return `<p class="card-sub retired-engine-note">Scored by the four-pillar engine, which
    has since been retired: two of its four pillars never saw the job description
    and returned the same number on every posting. Kept so this record still
    reads, not comparable with newer scores.</p>` + PILLAR_ORDER.map((key) => {
    const p = breakdown[key];
    if (!p) return "";
    const pct = Math.round(p.score * 100);
    return `
      <div class="score-pillar-row">
        <div class="score-pillar-head">
          <span class="score-pillar-name">${PILLAR_LABELS[key]}</span>
          <span class="score-pillar-meta">${pct}% &middot; weight ${Math.round(p.weight * 100)}%</span>
        </div>
        <div class="score-pillar-track"><div class="score-pillar-fill ${fillClass(p.score)}" style="width:${pct}%"></div></div>
        <p class="score-pillar-evidence">${escapeHtml(pillarEvidence(key, p))}</p>
      </div>
    `;
  }).join("");
}

function renderChips(breakdown) {
  let matched, missing;
  if (isRequirementsRecord(activeRecord)) {
    ({ matched, missing } = requirementChips(breakdown));
  } else {
    const kw = breakdown && breakdown.keyword_match;
    matched = (kw && kw.matched_skills) || [];
    missing = (kw && kw.missing_skills) || [];
  }
  return {
    matched: matched.length
      ? matched.map((s) => `<span class="keyword-chip chip-matched">${escapeHtml(s)}</span>`).join("")
      : `<span class="empty">None matched.</span>`,
    missing: missing.length
      ? missing.map((s) => `<span class="keyword-chip chip-missing">${escapeHtml(s)}</span>`).join("")
      : `<span class="empty">None missing.</span>`,
  };
}

function highlightMatches(text, skills) {
  let safe = escapeHtml(text);
  const sorted = (skills || []).filter(Boolean).slice().sort((a, b) => b.length - a.length);
  for (const skill of sorted) {
    const escaped = skill.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (!escaped) continue;
    const re = new RegExp(escaped, "gi");
    safe = safe.replace(re, (m) => `<mark class="hl-match">${m}</mark>`);
  }
  return safe;
}

function isPdfUrl(url) {
  return /\.pdf($|\?)/i.test(url || "");
}

// Cached across both tabs and repeated opens -- the original CV doesn't
// change while the dashboard/CV page is open, so there's no need to refetch
// it every time the modal opens.
let cachedCvStatusPromise = null;
function getCvStatus() {
  if (!cachedCvStatusPromise) {
    cachedCvStatusPromise = getJSON("/api/cv/status").catch(() => ({ has_cv: false }));
  }
  return cachedCvStatusPromise;
}

function renderDocPane(tab, record, cvStatus) {
  if (tab === "tailored") {
    if (!record.tailored_download_url) {
      return `<div class="doc-frame"><p class="doc-text-preview empty">No tailored CV was generated for this job — its fit score didn't fall below the rewrite threshold.</p></div>`;
    }
    const url = `${API_BASE}${record.tailored_download_url}`;
    return `<div class="doc-frame"><iframe src="${url}" title="Tailored CV"></iframe></div>`;
  }

  if (!cvStatus || !cvStatus.has_cv || !cvStatus.file_url) {
    return `<div class="doc-frame"><p class="doc-text-preview empty">Original CV file not available.</p></div>`;
  }

  if (isPdfUrl(cvStatus.file_url)) {
    const url = `${API_BASE}${cvStatus.file_url}`;
    return `<div class="doc-frame"><iframe src="${url}" title="Original CV"></iframe></div>`;
  }

  // .docx has no in-browser renderer here -- fall back to the parsed-text
  // preview with matched keywords highlighted.
  const matched = isRequirementsRecord(record)
    // Highlight the surface forms that actually matched, not the requirement
    // names -- a subset match means "CNN" is what's in the CV, not "Deep
    // Learning", and highlighting the latter would find nothing.
    ? requirementMatchedForms(record.ats_breakdown)
    : (record.ats_breakdown && record.ats_breakdown.keyword_match
        ? record.ats_breakdown.keyword_match.matched_skills
        : []);
  const excerpt = cvStatus.preview
    ? highlightMatches(cvStatus.preview, matched)
    : "No preview available.";
  return `
    <div class="doc-frame">
      <pre class="doc-text-preview">${excerpt}</pre>
      <div class="doc-frame-note">.docx files can't be rendered inline — showing the first ~400 parsed characters. <a href="${API_BASE}${cvStatus.file_url}" target="_blank" rel="noopener">Download the file</a> to view it in full.</div>
    </div>
  `;
}

let whyModal, whyModalClose, whyScoreValue, whyRatingBadge, whyDocPane, whyPillars, whyMatchedChips, whyMissingChips, whyTabButtons;
let activeRecord = null;
let activeCvStatus = null;
let activeTab = "original";
let boundOnce = false;

function bindModalOnce() {
  if (boundOnce) return;
  whyModal = document.getElementById("why-modal");
  if (!whyModal) return; // this page doesn't include the modal markup
  boundOnce = true;

  whyModalClose = document.getElementById("why-modal-close");
  whyScoreValue = document.getElementById("why-score-value");
  whyRatingBadge = document.getElementById("why-rating-badge");
  whyDocPane = document.getElementById("why-doc-pane");
  whyPillars = document.getElementById("why-pillars");
  whyMatchedChips = document.getElementById("why-matched-chips");
  whyMissingChips = document.getElementById("why-missing-chips");
  whyTabButtons = document.querySelectorAll(".modal-tab");

  whyModalClose.addEventListener("click", closeWhyModal);
  whyModal.addEventListener("click", (e) => { if (e.target === whyModal) closeWhyModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !whyModal.hidden) closeWhyModal(); });
  whyTabButtons.forEach((btn) => btn.addEventListener("click", () => {
    activeTab = btn.dataset.tab;
    renderWhyModal();
  }));
}

function renderWhyModal() {
  if (!activeRecord) return;
  whyTabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === activeTab));

  const score = activeTab === "tailored" ? activeRecord.tailored_ats_score : activeRecord.ats_score;
  const breakdown = activeTab === "tailored" ? activeRecord.tailored_ats_breakdown : activeRecord.ats_breakdown;

  whyScoreValue.textContent = fmtPct(score);
  const rk = ratingKey(score);
  whyRatingBadge.textContent = RATING_LABEL[rk];
  whyRatingBadge.className = `rating-badge rating-${rk}`;

  whyDocPane.innerHTML = renderDocPane(activeTab, activeRecord, activeCvStatus);
  whyPillars.innerHTML = renderPillars(breakdown);
  const chips = renderChips(breakdown);
  whyMatchedChips.innerHTML = chips.matched;
  whyMissingChips.innerHTML = chips.missing;
}

async function openWhyModal(record) {
  bindModalOnce();
  if (!whyModal) return; // page doesn't include the modal markup -- nothing to do

  activeRecord = record;
  activeTab = "original";
  whyModal.hidden = false;
  document.body.style.overflow = "hidden";
  renderWhyModal(); // render immediately with what we already have; CV status fills in below

  const thisRecord = record;
  activeCvStatus = await getCvStatus();
  if (activeRecord === thisRecord) renderWhyModal(); // guard against a second open racing this fetch
}
window.openWhyModal = openWhyModal;

function closeWhyModal() {
  if (!whyModal) return;
  whyModal.hidden = true;
  whyDocPane.innerHTML = ""; // drop any iframe so it stops loading in the background
  document.body.style.overflow = "";
}
