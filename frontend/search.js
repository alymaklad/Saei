// Search page: the role query and filters, job-board management, running a
// search, and the best recent matches.

const SITE_TYPE_LABEL = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  generic: "Career page",
  wuzzuf: "Job board",
  bayt: "Job board",
};

const positionInput = document.getElementById("position-input");
const seniorityInput = document.getElementById("seniority-input");
const maxAgeInput = document.getElementById("max-age-input");
const maxResultsInput = document.getElementById("max-results-input");
const positionMessage = document.getElementById("position-message");

// ---- filters ------------------------------------------------------------------

async function loadPosition() {
  try {
    const cfg = await getJSON("/api/search/config");
    positionInput.value = cfg.position_query || "";
    // Seniority options come from the backend (agents/search_agent.py's
    // SENIORITY_LABELS) so the control can't drift from what the filter knows.
    (cfg.seniority_levels || []).forEach(({ value, label }) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      seniorityInput.appendChild(opt);
    });
    seniorityInput.value = cfg.seniority_level || "";
    maxAgeInput.value = cfg.max_age_days ? String(cfg.max_age_days) : "";
    maxResultsInput.value = cfg.max_results_per_site ? String(cfg.max_results_per_site) : "";
  } catch (e) {
    positionMessage.textContent = "Could not reach the backend API.";
    positionMessage.className = "save-message save-error mt-space-sm block";
  }
}

function saveFilters() {
  return postJSON("/api/search/config", {
    position_query: positionInput.value.trim(),
    seniority_level: seniorityInput.value,
    max_age_days: maxAgeInput.value ? parseInt(maxAgeInput.value, 10) : null,
    max_results_per_site: maxResultsInput.value ? parseInt(maxResultsInput.value, 10) : null,
  });
}

// ---- sources ------------------------------------------------------------------

const sitesBody = document.getElementById("sites-body");
const siteMessage = document.getElementById("site-message");

function siteTile(site) {
  const typeLabel = SITE_TYPE_LABEL[site.site_type] || site.site_type;
  return `
    <div class="flex items-center justify-between gap-2 rounded-lg bg-surface-container-low px-space-sm py-2">
      <div class="min-w-0">
        <a href="${escapeHtml(site.url)}" target="_blank" rel="noopener" title="${escapeHtml(site.url)}"
           class="block truncate text-label-md text-on-surface hover:text-primary">${escapeHtml(site.label || site.url)}</a>
        <span class="text-label-sm normal-case tracking-normal text-on-surface-variant">${escapeHtml(typeLabel)}</span>
      </div>
      <button type="button" class="sa-icon-btn remove-site-btn" data-id="${site.id}" aria-label="Remove source" title="Remove">
        <span class="ms text-[18px]">close</span>
      </button>
    </div>`;
}

async function loadSites() {
  try {
    const sites = await getJSON("/api/search/sites");
    const names = sites.map((s) => s.label || s.url);
    document.getElementById("sources-summary").textContent = sites.length
      ? `${sites.length} job board${sites.length === 1 ? "" : "s"} active (${names.join(", ")})`
      : "none added yet — Greenhouse, Lever and the always-on feeds still run";
    sitesBody.innerHTML = sites.length ? sites.map(siteTile).join("")
      : `<p class="sa-empty col-span-3">No sources added yet.</p>`;
    sitesBody.querySelectorAll(".remove-site-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await deleteJSON(`/api/search/sites/${btn.dataset.id}`);
          loadSites();
        } catch (err) {
          siteMessage.textContent = err.message || "Could not remove source.";
          siteMessage.className = "save-message save-error mt-2 block";
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    document.getElementById("sources-summary").textContent = "could not reach the backend API";
  }
}

document.getElementById("manage-sources").addEventListener("click", () => {
  const panel = document.getElementById("sources-panel");
  panel.hidden = !panel.hidden;
});

document.getElementById("add-site-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("site-url-input");
  const url = input.value.trim();
  if (!url) return;
  siteMessage.textContent = "Adding…";
  siteMessage.className = "save-message mt-2 block";
  try {
    await postJSON("/api/search/sites", { url });
    input.value = "";
    siteMessage.textContent = "Added.";
    siteMessage.className = "save-message save-success mt-2 block";
    loadSites();
  } catch (err) {
    siteMessage.textContent = err.message || "Could not add source.";
    siteMessage.className = "save-message save-error mt-2 block";
  }
});

// ---- run ----------------------------------------------------------------------
// The run is one blocking request, so there are no real per-stage progress
// events. While it's in flight the bar is indeterminate; the step cards only
// get numbers once the result is back.

const RUN_STEPS = [
  { key: "find", label: "Finding opportunities" },
  { key: "match", label: "Matching against profile" },
  { key: "prepare", label: "Preparing your results" },
];

function paintRun(state, detail = {}) {
  const panel = document.getElementById("run-panel");
  panel.hidden = false;
  const bar = document.getElementById("run-bar");
  const chip = document.getElementById("run-chip");
  const title = document.getElementById("run-title");
  if (state === "running") {
    title.innerHTML = "Sa'ei is searching across your active sources&hellip;";
    chip.innerHTML = `<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-tertiary"></span>Running`;
    bar.style.width = "60%";
    bar.classList.add("animate-pulse");
  } else {
    title.textContent = state === "failed" ? "The search finished with problems" : "Search complete";
    chip.innerHTML = `<span class="h-1.5 w-1.5 rounded-full ${state === "failed" ? "bg-error" : "bg-secondary"}"></span>${state === "failed" ? "Needs attention" : "Done"}`;
    bar.style.width = "100%";
    bar.classList.remove("animate-pulse");
  }
  document.getElementById("run-steps").innerHTML = RUN_STEPS.map((s) => {
    const text = detail[s.key];
    const done = state !== "running";
    return `
      <div class="flex items-start gap-2 rounded-lg ${done ? "bg-surface-container-lowest/70" : "bg-surface-container-lowest"} p-space-sm">
        <span class="ms mt-0.5 text-[18px] ${done ? (state === "failed" && s.key === "prepare" ? "text-error" : "text-primary") : "animate-pulse text-on-surface-variant"}">${done ? "check_circle" : "radio_button_unchecked"}</span>
        <div>
          <p class="text-label-md font-semibold text-on-surface">${s.label}</p>
          <p class="text-body-sm text-on-surface-variant">${escapeHtml(text || (done ? "Done" : "In progress"))}</p>
        </div>
      </div>`;
  }).join("");
}

function summarizeJobErrors(jobErrors) {
  // The same failure (e.g. a rate limit) usually repeats across every job,
  // but messages like "Used 197574 ... try again in 17m13s" carry per-request
  // numbers -- group with digits blanked so those still collapse together.
  const groups = new Map();
  jobErrors.forEach((e) => {
    const msg = e.error || "Unknown error";
    const key = msg.replace(/\d+(\.\d+)?/g, "#");
    if (!groups.has(key)) groups.set(key, { count: 0, sample: msg });
    groups.get(key).count += 1;
  });
  return [...groups.values()].map(({ sample, count }) => {
    // Rate-limit/quota messages are long and mostly marketing copy -- show
    // just the useful lead-in.
    const isRateLimit = /rate.?limit|quota|429/i.test(sample);
    const shown = isRateLimit ? (sample.match(/Rate limit reached[^.]*\./i)?.[0] || sample.slice(0, 200)) : sample;
    return count > 1 ? `${shown} (x${count})` : shown;
  }).join("; ");
}

const searchBtn = document.getElementById("search-btn");
const searchMessage = document.getElementById("search-message");

document.getElementById("position-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  searchBtn.disabled = true;
  positionMessage.textContent = "";
  try {
    // Search uses what's saved, so save the filters as typed first.
    await saveFilters();
  } catch (err) {
    positionMessage.textContent = err.message || "Could not save the filters.";
    positionMessage.className = "save-message save-error mt-space-sm block";
    searchBtn.disabled = false;
    return;
  }
  paintRun("running");
  searchMessage.innerHTML = `<span class="ms text-[16px]">info</span><span>Takes a few minutes — one model call per new job found.</span>`;
  searchMessage.className = "mt-space-md flex items-start gap-2 text-body-sm text-on-surface-variant";
  try {
    const result = await postJSON("/api/search/run", {});
    const sourceErrors = result.source_errors || [];
    const jobErrors = result.errors || [];
    const failed = jobErrors.length || sourceErrors.length;
    paintRun(failed ? "failed" : "done", {
      find: `${result.found} role${result.found === 1 ? "" : "s"} found${sourceErrors.length ? ` · ${sourceErrors.length} source(s) unreachable` : ""}`,
      match: `${result.processed.length} new after deduplication`,
      prepare: jobErrors.length ? `${jobErrors.length} job(s) failed` : "Ready below and on Applications",
    });
    const notes = [];
    if (sourceErrors.length) notes.push("Unreachable: " + sourceErrors.map((x) => `${x.source}:${x.identifier || "?"} — ${x.error}`).join("; "));
    if (jobErrors.length) notes.push("Job failures: " + summarizeJobErrors(jobErrors));
    if (notes.length) {
      searchMessage.innerHTML = `<span class="ms text-[16px]">error</span><span>${escapeHtml(notes.join(" "))}</span>`;
      searchMessage.className = "mt-space-md flex items-start gap-2 text-body-sm text-error";
    } else {
      searchMessage.innerHTML = `<span class="ms text-[16px]">check</span><span>Finished. New roles are ranked below and on the Applications page.</span>`;
    }
    loadSnapshots();
  } catch (err) {
    paintRun("failed", { prepare: "The search request failed" });
    searchMessage.innerHTML = `<span class="ms text-[16px]">error</span><span>${escapeHtml(err.message || "Search failed.")}</span>`;
    searchMessage.className = "mt-space-md flex items-start gap-2 text-body-sm text-error";
  } finally {
    searchBtn.disabled = false;
  }
});

// ---- snapshots --------------------------------------------------------------------

const SOURCE_ICON = {
  greenhouse: "eco", generic: "language", weworkremotely: "public", lever: "work", ashby: "work",
  smartrecruiters: "work", linkedin: "group", himalayas: "public", remotive: "public", jobicy: "public",
  workingnomads: "public", remoteok: "public",
};

function snapshotCard(a) {
  const p = pct(a.match_score);
  const tone = p === null ? "text-on-surface-variant" : p >= 75 ? "text-primary" : "text-tertiary";
  return `
    <a href="applications.html?id=${a.id}" class="flex items-center gap-space-md rounded-xl bg-surface-container-lowest p-space-md shadow-soft transition-shadow hover:shadow-md">
      <span class="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-lg bg-surface-container text-primary"><span class="ms">${SOURCE_ICON[a.source] || "work"}</span></span>
      <div class="min-w-0 flex-1">
        <p class="truncate text-headline-sm text-on-surface">${escapeHtml(a.title || "Untitled role")} <span class="text-label-sm normal-case tracking-normal text-on-surface-variant">· ${escapeHtml(a.company || "Unknown company")}</span></p>
        <p class="flex flex-wrap items-center gap-x-2 text-body-sm text-on-surface-variant">
          <span class="flex items-center gap-0.5"><span class="ms text-[16px]">travel_explore</span>${escapeHtml(a.source || "—")}</span>
          <span>·</span><span>${timeAgo(a.date_created)}</span>
          ${a.ats_score != null ? `<span>·</span><span class="font-mono text-label-sm normal-case tracking-normal">ATS ${pct(hasTailored(a) ? a.tailored_ats_score : a.ats_score)}%</span>` : ""}
        </p>
      </div>
      <div class="text-right">
        <p class="text-headline-md ${tone}">${p === null ? "—" : p + "%"}</p>
        <p class="text-label-sm uppercase tracking-wider text-on-surface-variant">Match</p>
      </div>
      <span class="flex h-8 w-8 items-center justify-center rounded-lg bg-surface-container text-on-surface-variant"><span class="ms text-[18px]">chevron_right</span></span>
    </a>`;
}

async function loadSnapshots() {
  const el = document.getElementById("snapshots");
  try {
    const apps = await getJSON("/api/applications?limit=50");
    const best = sortApplicationsBy(apps, "match_score").slice(0, 5);
    document.getElementById("snap-chip").textContent = best.length ? `Top ${best.length}` : "";
    el.innerHTML = best.length ? best.map(snapshotCard).join("")
      : `<p class="sa-empty">Nothing found yet — run a search.</p>`;
  } catch (e) {
    el.innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
  }
}

loadPosition();
loadSites();
loadSnapshots();
