// Search tab: position query, job-board site list, and the search-now button.

const SITE_TYPE_LABEL = { greenhouse: "Greenhouse", lever: "Lever", generic: "Generic" };

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---- position ---------------------------------------------------------

const positionInput = document.getElementById("position-input");
const seniorityInput = document.getElementById("seniority-input");
const maxAgeInput = document.getElementById("max-age-input");
const maxResultsInput = document.getElementById("max-results-input");
const positionMessage = document.getElementById("position-message");

async function loadPosition() {
  try {
    const cfg = await getJSON("/api/search/config");
    positionInput.value = cfg.position_query || "";

    // Seniority options come from the backend (agents/search_agent.py's
    // SENIORITY_LABELS) so the dropdown can't drift out of sync with what
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

function siteRow(site) {
  const typeLabel = SITE_TYPE_LABEL[site.site_type] || site.site_type;
  return `
    <tr>
      <td><a href="${site.url}" target="_blank" rel="noopener">${escapeHtml(site.label || site.url)}</a></td>
      <td><span class="status-tag status-pending_review">${escapeHtml(typeLabel)}</span></td>
      <td>${fmtDate(site.date_added)}</td>
      <td><button type="button" class="btn remove-site-btn" data-id="${site.id}">Remove</button></td>
    </tr>`;
}

async function loadSites() {
  try {
    const sites = await getJSON("/api/search/sites");
    if (!sites.length) {
      sitesBody.innerHTML = `<tr><td colspan="4" class="empty">No sites added yet.</td></tr>`;
      return;
    }
    sitesBody.innerHTML = sites.map(siteRow).join("");
    sitesBody.querySelectorAll(".remove-site-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await deleteJSON(`/api/search/sites/${btn.dataset.id}`);
          loadSites();
        } catch (err) {
          siteMessage.textContent = err.message || "Could not remove site.";
          siteMessage.className = "save-message save-error";
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    sitesBody.innerHTML = `<tr><td colspan="4" class="empty">Could not reach the backend API.</td></tr>`;
  }
}

document.getElementById("add-site-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("site-url-input");
  const url = input.value.trim();
  if (!url) return;

  siteMessage.textContent = "Adding…";
  siteMessage.className = "save-message";
  try {
    await postJSON("/api/search/sites", { url });
    input.value = "";
    siteMessage.textContent = "Added.";
    siteMessage.className = "save-message save-success";
    loadSites();
  } catch (err) {
    siteMessage.textContent = err.message || "Could not add site.";
    siteMessage.className = "save-message save-error";
  }
});

// ---- run search now -------------------------------------------------------

const searchBtn = document.getElementById("search-btn");
const searchMessage = document.getElementById("search-message");

searchBtn.addEventListener("click", async () => {
  searchBtn.disabled = true;
  searchMessage.textContent = "Searching… this can take a few minutes (one LLM call per new job found).";
  searchMessage.className = "save-message search-message";

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
    searchMessage.textContent = text;
    searchMessage.className = (jobErrors.length || sourceErrors.length)
      ? "save-message search-message save-error"
      : "save-message search-message save-success";
  } catch (e) {
    searchMessage.textContent = e.message || "Search failed.";
    searchMessage.className = "save-message search-message save-error";
  } finally {
    searchBtn.disabled = false;
  }
});

loadPosition();
loadSites();
