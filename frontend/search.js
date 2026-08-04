// Search tab: position query, job-board site list, and the search-now button.

const SITE_TYPE_LABEL = { greenhouse: "Greenhouse", lever: "Lever", generic: "Generic" };

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---- position ---------------------------------------------------------

const positionInput = document.getElementById("position-input");
const positionMessage = document.getElementById("position-message");

async function loadPosition() {
  try {
    const cfg = await getJSON("/api/search/config");
    positionInput.value = cfg.position_query || "";
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
    await postJSON("/api/search/config", { position_query: positionInput.value.trim() });
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
    const parts = [`${result.found} job(s) found`, `${result.processed.length} new`];
    if (result.errors.length) parts.push(`${result.errors.length} failed`);
    searchMessage.textContent = parts.join(", ") + ".";
    searchMessage.className = result.errors.length
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
