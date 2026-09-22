// Search tab: position query, job-board site list, the search-now button, and
// the most recent discoveries.

const SITE_TYPE_LABEL = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  generic: "Generic",
  wuzzuf: "Job board",
  bayt: "Job board",
};

// ---- position ---------------------------------------------------------

const positionInput = document.getElementById("position-input");
const seniorityInput = document.getElementById("seniority-input");
const seniorityPills = document.getElementById("seniority-pills");
const maxAgeInput = document.getElementById("max-age-input");
const maxResultsInput = document.getElementById("max-results-input");
const positionMessage = document.getElementById("position-message");

// The hidden <select> stays the source of truth (it's what gets saved); the
// pills are just a friendlier way to set it.
function renderSeniorityPills() {
  seniorityPills.innerHTML = [...seniorityInput.options].map((o) => {
    const on = o.value === seniorityInput.value;
    return `<button type="button" data-value="${escapeHtml(o.value)}"
      class="rounded-lg px-4 py-2 text-label-lg transition-colors ${on
        ? "bg-primary text-on-primary shadow-sm"
        : "bg-surface-container-high text-on-surface hover:bg-surface-container-highest"}">${escapeHtml(o.textContent)}</button>`;
  }).join("");
}

seniorityPills.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-value]");
  if (!btn) return;
  seniorityInput.value = btn.dataset.value;
  renderSeniorityPills();
});

async function loadPosition() {
  try {
    const cfg = await getJSON("/api/search/config");
    positionInput.value = cfg.position_query || "";

    // Seniority options come from the backend (agents/search_agent.py's
    // SENIORITY_LABELS) so the control can't drift out of sync with what
    // the filter actually understands.
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
    positionMessage.className = "save-message save-error";
  }
  renderSeniorityPills();
}

document.getElementById("position-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  positionMessage.textContent = "Saving…";
  positionMessage.className = "save-message";
  try {
    await postJSON("/api/search/config", {
      position_query: positionInput.value.trim(),
      seniority_level: seniorityInput.value,
      max_age_days: maxAgeInput.value ? parseInt(maxAgeInput.value, 10) : null,
      max_results_per_site: maxResultsInput.value ? parseInt(maxResultsInput.value, 10) : null,
    });
    positionMessage.textContent = "Saved.";
    positionMessage.className = "save-message save-success";
  } catch (err) {
    positionMessage.textContent = err.message || "Could not save.";
    positionMessage.className = "save-message save-error";
  }
});

// ---- sites --------------------------------------------------------------

const sitesBody = document.getElementById("sites-body");
const siteMessage = document.getElementById("site-message");

function siteTile(site) {
  const typeLabel = SITE_TYPE_LABEL[site.site_type] || site.site_type;
  return `
    <div class="flex items-center justify-between gap-2 rounded-2xl bg-surface-container-low p-3.5">
      <div class="flex min-w-0 items-center gap-2.5">
        <span class="h-2 w-2 flex-shrink-0 rounded-full ${site.site_type === "generic" ? "bg-tertiary" : "bg-secondary"}"></span>
        <div class="min-w-0">
          <a href="${escapeHtml(site.url)}" target="_blank" rel="noopener"
             class="block truncate text-label-lg text-on-surface hover:text-primary" title="${escapeHtml(site.url)}">${escapeHtml(site.label || site.url)}</a>
          <span class="text-label-sm text-on-surface-variant">${escapeHtml(typeLabel)} · added ${fmtDate(site.date_added)}</span>
        </div>
      </div>
      <button type="button" class="sa-icon-btn remove-site-btn" data-id="${site.id}" aria-label="Remove site" title="Remove">
        <span class="ms text-[18px]">delete</span>
      </button>
    </div>`;
}

async function loadSites() {
  try {
    const sites = await getJSON("/api/search/sites");
    document.getElementById("coverage-count").textContent = sites.length;
    document.getElementById("sites-count").textContent = `${sites.length} site${sites.length === 1 ? "" : "s"}`;
    if (!sites.length) {
      sitesBody.innerHTML = `<p class="sa-empty sm:col-span-2">No sites added yet.</p>`;
      return;
    }
    sitesBody.innerHTML = sites.map(siteTile).join("");
    sitesBody.querySelectorAll(".remove-site-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await deleteJSON(`/api/search/sites/${btn.dataset.id}`);
          loadSites();
        } catch (err) {
          siteMessage.textContent = err.message || "Could not remove site.";
          siteMessage.className = "save-message save-error mt-2 block";
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    sitesBody.innerHTML = `<p class="sa-empty sm:col-span-2">Could not reach the backend API.</p>`;
  }
}

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
    siteMessage.textContent = err.message || "Could not add site.";
    siteMessage.className = "save-message save-error mt-2 block";
  }
});

// ---- run status stepper ----------------------------------------------------
// The run is one blocking request, so there are no real per-stage progress
// events to show. While it's in flight the whole pipeline pulses as "running";
// afterwards every stage reads done (or the run reads failed).

const RUN_STEPS = [
  { icon: "manage_search", label: "Expanding query" },
  { icon: "travel_explore", label: "Sources" },
  { icon: "hub", label: "Semantic match" },
  { icon: "fact_check", label: "ATS scoring" },
  { icon: "flag", label: "Prioritizing" },
];

function renderRunSteps(state) {
  document.getElementById("run-steps").innerHTML = RUN_STEPS.map((s, i) => {
    let dot;
    if (state === "done") {
      dot = `<div class="flex h-9 w-9 items-center justify-center rounded-full bg-secondary text-on-secondary shadow-sm"><span class="ms text-[18px]">check</span></div>`;
    } else if (state === "running") {
      dot = `<div class="flex h-9 w-9 animate-pulse items-center justify-center rounded-full bg-primary text-on-primary shadow-md ring-4 ring-primary/20" style="animation-delay:${i * 200}ms"><span class="ms text-[18px]">${s.icon}</span></div>`;
    } else if (state === "failed") {
      dot = `<div class="flex h-9 w-9 items-center justify-center rounded-full bg-error-container text-error"><span class="ms text-[18px]">${s.icon}</span></div>`;
    } else {
      dot = `<div class="flex h-9 w-9 items-center justify-center rounded-full bg-surface-container-highest text-on-surface-variant"><span class="ms text-[18px]">${s.icon}</span></div>`;
    }
    return `<div class="flex flex-col items-center gap-2 text-center">${dot}<span class="text-label-sm font-semibold text-on-surface">${s.label}</span></div>`;
  }).join("");
  const chip = document.getElementById("run-state-chip");
  const map = {
    idle: ["sa-chip", "Idle"],
    running: ["sa-chip-primary", "Running…"],
    done: ["sa-chip-teal", "Last run complete"],
    failed: ["sa-chip-error", "Last run had errors"],
  };
  chip.className = map[state][0];
  chip.textContent = map[state][1];
}
renderRunSteps("idle");

// ---- run search now -------------------------------------------------------

const searchBtn = document.getElementById("search-btn");
const searchMessage = document.getElementById("search-message");
const MSG_CLASS = "save-message mt-5 block rounded-xl bg-surface-container-lowest px-4 py-3 font-mono text-label-md";

searchBtn.addEventListener("click", async () => {
  searchBtn.disabled = true;
  renderRunSteps("running");
  searchMessage.textContent = "Searching… this can take a few minutes (one LLM call per new job found).";
  searchMessage.className = MSG_CLASS;

  try {
    const result = await postJSON("/api/search/run", {});
    const sourceErrors = result.source_errors || [];
    const jobErrors = result.errors || [];
    const parts = [`${result.found} job(s) found`, `${result.processed.length} new`];
    if (jobErrors.length) parts.push(`${jobErrors.length} failed`);
    if (sourceErrors.length) parts.push(`${sourceErrors.length} source(s) unreachable`);
    let text = parts.join(", ") + ".";
    if (sourceErrors.length) {
      text += " " + sourceErrors.map((e) => `${e.source}:${e.identifier || "?"} — ${e.error}`).join("; ");
    }
    if (jobErrors.length) {
      // The same failure (e.g. a rate limit) usually repeats across every
      // job, but messages like "Used 197574 ... try again in 17m13s" carry
      // per-request numbers that make each one a unique string -- group by
      // the message with digits blanked out so those still collapse together.
      const groups = new Map();
      jobErrors.forEach((e) => {
        const msg = e.error || "Unknown error";
        const key = msg.replace(/\d+(\.\d+)?/g, "#");
        if (!groups.has(key)) groups.set(key, { count: 0, sample: msg });
        groups.get(key).count += 1;
      });
      const summary = [...groups.values()]
        .map(({ sample, count }) => {
          // Rate-limit/quota messages are long and mostly marketing copy --
          // show just the useful lead-in instead of the full raw text.
          const isRateLimit = /rate.?limit|quota|429/i.test(sample);
          const shown = isRateLimit
            ? (sample.match(/Rate limit reached[^.]*\./i)?.[0] || sample.slice(0, 200))
            : sample;
          return count > 1 ? `${shown} (x${count})` : shown;
        })
        .join("; ");
      text += ` Job failures: ${summary}`;
    }
    const failed = jobErrors.length || sourceErrors.length;
    searchMessage.textContent = text;
    searchMessage.className = `${MSG_CLASS} ${failed ? "save-error" : "save-success"}`;
    renderRunSteps(failed ? "failed" : "done");
    loadDiscoveries();
  } catch (e) {
    searchMessage.textContent = e.message || "Search failed.";
    searchMessage.className = `${MSG_CLASS} save-error`;
    renderRunSteps("failed");
  } finally {
    searchBtn.disabled = false;
  }
});

// ---- discoveries ------------------------------------------------------------

let discoveries = [];
let triage = "all";
let shown = 5;

function discoveryCard(a) {
  return `
    <article class="rounded-3xl bg-surface-container-lowest p-5 shadow-sm transition-shadow hover:shadow-md">
      <div class="flex items-start gap-4">
        ${companyAvatar(a.company, "sa-avatar h-12 w-12")}
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div class="min-w-0">
              <h3 class="text-headline-sm font-bold text-on-surface">${escapeHtml(a.title || "Untitled role")}</h3>
              <div class="mt-1 flex flex-wrap items-center gap-2">
                <span class="sa-chip-teal">${escapeHtml(a.source || "—")}</span>
                <span class="text-label-lg text-on-surface">${escapeHtml(a.company || "Unknown company")}</span>
              </div>
            </div>
            <div class="flex flex-col items-end gap-1">
              ${matchChip(a.match_score)}
              ${atsChip(a.ats_score, a.tailored_ats_score)}
            </div>
          </div>
          <div class="mt-4 flex flex-wrap items-center justify-between gap-3">
            <div class="flex flex-wrap items-center gap-3 text-label-sm text-on-surface-variant">
              <span class="flex items-center gap-1"><span class="ms text-[16px]">schedule</span>${timeAgo(a.date_created)}</span>
              ${statusChip(a.status)}
            </div>
            <div class="flex items-center gap-2">
              <a href="applications.html?id=${a.id}" class="sa-btn sa-btn-sm"><span class="ms text-[16px]">visibility</span>Review &amp; audit</a>
              <a href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener" class="sa-btn-primary sa-btn-sm"><span class="ms text-[16px]">open_in_new</span>Open posting</a>
            </div>
          </div>
        </div>
      </div>
    </article>`;
}

function renderDiscoveries() {
  const sortKey = document.getElementById("discovery-sort").value;
  let list = discoveries;
  if (triage === "high") list = list.filter((a) => (a.match_score || 0) >= 0.75);
  if (triage === "tailored") list = list.filter(hasTailored);
  list = sortApplicationsBy(list, sortKey);

  const el = document.getElementById("discoveries");
  el.innerHTML = list.length
    ? list.slice(0, shown).map(discoveryCard).join("")
    : `<p class="sa-empty">${discoveries.length ? "Nothing in this view." : "No discoveries yet — launch a search."}</p>`;
  document.getElementById("discoveries-footer").innerHTML =
    `Showing <strong>${Math.min(shown, list.length)}</strong> of <strong>${list.length}</strong> recent discoveries`;
  document.getElementById("discoveries-more").hidden = shown >= list.length;
}

async function loadDiscoveries() {
  try {
    discoveries = await getJSON("/api/applications?limit=100");
    renderDiscoveries();
  } catch (e) {
    document.getElementById("discoveries").innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
  }
}

bindTabs(document.getElementById("triage-tabs"), (f) => { triage = f; shown = 5; renderDiscoveries(); });
document.getElementById("discovery-sort").addEventListener("change", renderDiscoveries);
document.getElementById("discoveries-more").addEventListener("click", () => { shown += 5; renderDiscoveries(); });

loadPosition();
loadSites();
loadDiscoveries();
