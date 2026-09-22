// Reports page: skill gaps, the latest news digest, and report history.

// ---- skill gaps -----------------------------------------------------------------

async function loadSkillGaps() {
  const list = document.getElementById("skill-gaps-list");
  try {
    const gaps = await getJSON("/api/skill-gaps?limit=12");
    document.getElementById("stat-gaps").textContent = gaps.length;
    document.getElementById("gaps-chip").textContent = gaps.length ? `${gaps.length} gaps cataloged` : "";
    if (!gaps.length) {
      list.innerHTML = `<li class="sa-empty md:col-span-2">No recurring gaps yet.</li>`;
      document.getElementById("stat-topgap").textContent = "0";
      return;
    }
    const max = gaps[0].count;
    const total = gaps.reduce((s, g) => s + g.count, 0);
    document.getElementById("stat-gaps-sub").textContent = `${total} missing-skill mentions in total`;
    document.getElementById("stat-topgap").textContent = gaps[0].count;
    document.getElementById("stat-topgap-sub").textContent = `would credit "${gaps[0].skill}"`;

    // Highlight the top one or two -- the highest-leverage things to learn.
    const top = gaps.slice(0, 2);
    const hl = document.getElementById("gap-highlight");
    hl.classList.remove("hidden");
    hl.classList.add("flex");
    hl.innerHTML = `
      <div class="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-full bg-primary text-on-primary"><span class="ms">auto_graph</span></div>
      <div class="flex-1">
        <h3 class="text-headline-sm font-bold text-on-surface">High-leverage opportunity: ${top.map((g) => escapeHtml(g.skill)).join(" &amp; ")}</h3>
        <p class="text-body-sm text-on-surface-variant">
          ${top.length === 2
            ? `These two were missing from <strong class="text-on-surface">${top[0].count}</strong> and <strong class="text-on-surface">${top[1].count}</strong> matched postings`
            : `Missing from <strong class="text-on-surface">${top[0].count}</strong> matched postings`} —
          the most common reason your CV lost points. Demonstrating them in a role or project on your Profile is what earns full credit.
        </p>
      </div>
      <a href="profile.html" class="sa-btn-primary sa-btn-sm self-center whitespace-nowrap"><span class="ms text-[16px]">bolt</span>Add to profile</a>`;

    list.innerHTML = gaps.map((g, i) => {
      const w = Math.round((g.count / max) * 100);
      const tone = i < 3 ? "bg-primary" : i < 6 ? "bg-primary-container/80" : "bg-primary-fixed-dim";
      return `
        <li>
          <div class="mb-1.5 flex items-center justify-between gap-3">
            <span class="flex items-center gap-2 text-label-lg text-on-surface"><span class="h-1.5 w-1.5 rounded-full ${tone}"></span>${escapeHtml(g.skill)}</span>
            <span class="font-mono text-label-md text-on-surface">${g.count} job${g.count === 1 ? "" : "s"}</span>
          </div>
          <div class="sa-progress h-2"><div class="${tone}" style="width:${w}%"></div></div>
        </li>`;
    }).join("");
  } catch (e) {
    list.innerHTML = `<li class="sa-empty md:col-span-2">Could not reach the backend API.</li>`;
  }
}

// ---- news digest ------------------------------------------------------------------

async function loadNews() {
  const el = document.getElementById("news-content");
  try {
    const digests = await getJSON("/api/news?limit=1");
    if (!digests.length) {
      el.innerHTML = `<p class="sa-empty">No digest yet — runs weekly.</p>`;
      return;
    }
    const d = digests[0];
    el.innerHTML = `
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <span class="rounded bg-secondary-container px-2 py-0.5 font-mono text-label-sm font-bold text-on-secondary-container">LATEST EDITION</span>
        <span class="font-mono text-label-sm text-on-surface-variant">${d.date_created ? new Date(d.date_created).toLocaleString() : ""}</span>
      </div>
      <h3 class="mb-3 text-headline-md font-bold text-on-surface">${escapeHtml(d.field ? `${d.field} — weekly pulse` : "Weekly pulse")}</h3>
      <div class="max-h-96 overflow-y-auto whitespace-pre-wrap pr-2 text-body-md leading-7 text-on-surface-variant">${escapeHtml(d.content)}</div>`;
  } catch (e) {
    el.innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
  }
}

// ---- report history ---------------------------------------------------------------

const REPORTS_PREVIEW_COUNT = 6;
let allReports = [];
let reportFilter = "";

function reportCardHTML(r) {
  const failed = r.status === "failed";
  const title = r.report_type === "daily" ? "Daily Summary" : "Weekly Digest";
  const status = failed
    ? `<span class="sa-chip-error">failed</span>`
    : `<span class="sa-chip-teal"><span class="h-1.5 w-1.5 rounded-full bg-secondary"></span>${escapeHtml(r.status)}${r.dry_run ? " (dry run)" : ""}</span>`;
  return `
    <article class="sa-card">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-surface-container pb-3">
        <div class="flex flex-wrap items-center gap-2">
          <span class="rounded bg-surface-container-high px-2 py-0.5 font-mono text-label-sm font-bold uppercase text-on-surface">${escapeHtml(r.report_type)}</span>
          ${status}
        </div>
        <div class="flex items-center gap-2 font-mono text-label-sm text-on-surface-variant">
          <span class="ms text-[16px] text-secondary">send</span>
          <time>${r.sent_at ? new Date(r.sent_at).toLocaleString() : "—"}</time>
        </div>
      </div>
      <h4 class="text-headline-sm font-bold text-on-surface">${title}</h4>
      ${r.error_message ? `<p class="mt-1 text-body-sm text-error">${escapeHtml(r.error_message)}</p>` : ""}
      <details class="group mt-2">
        <summary class="cursor-pointer list-none text-label-md font-semibold text-primary">
          <span class="group-open:hidden">Show message &darr;</span><span class="hidden group-open:inline">Hide message &uarr;</span>
        </summary>
        <pre class="mt-3 max-h-80 overflow-auto whitespace-pre-wrap rounded-xl bg-surface-container-low p-4 font-sans text-body-sm text-on-surface-variant">${escapeHtml(r.content)}</pre>
      </details>
    </article>`;
}

function renderReports(reports, expanded) {
  const el = document.getElementById("reports-list");
  const viewAllRow = document.getElementById("reports-view-all-row");
  const viewAllLabel = document.getElementById("reports-view-all-label");

  if (!reports.length) {
    el.innerHTML = `<p class="sa-empty">${allReports.length ? "No reports of this type." : "No reports sent yet — the daily and weekly triggers haven't run, or the scheduler isn't running."}</p>`;
    viewAllRow.hidden = true;
    return;
  }

  const visible = expanded ? reports : reports.slice(0, REPORTS_PREVIEW_COUNT);
  el.innerHTML = visible.map(reportCardHTML).join("");

  if (!expanded && reports.length > REPORTS_PREVIEW_COUNT) {
    viewAllLabel.textContent = `Load older reports (${reports.length - REPORTS_PREVIEW_COUNT} more)`;
    viewAllRow.hidden = false;
  } else {
    viewAllRow.hidden = true;
  }
}

function applyReportFilter() {
  const filtered = reportFilter ? allReports.filter((r) => r.report_type === reportFilter) : allReports;
  renderReports(filtered, false);
  document.getElementById("reports-view-all-btn").onclick = () => renderReports(filtered, true);
}

async function loadReports() {
  const el = document.getElementById("reports-list");
  try {
    allReports = await getJSON("/api/reports?limit=50");
    const delivered = allReports.filter((r) => r.status !== "failed").length;
    document.getElementById("stat-delivered").textContent = delivered;
    document.getElementById("stat-failed").textContent = allReports.length - delivered;
    document.getElementById("stat-delivered-rate").textContent = allReports.length
      ? `${Math.round((delivered / allReports.length) * 100)}% success` : "";
    document.querySelector('#report-tabs [data-count=""]').textContent = `(${allReports.length})`;
    const last = allReports.find((r) => r.sent_at);
    document.getElementById("last-dispatch").textContent = last
      ? `${last.report_type === "daily" ? "Daily summary" : "Weekly digest"} · ${timeAgo(last.sent_at)}` : "None yet";
    applyReportFilter();
  } catch (e) {
    el.innerHTML = `<p class="sa-empty">Could not reach the backend API.</p>`;
    document.getElementById("reports-view-all-row").hidden = true;
  }
}

bindTabs(document.getElementById("report-tabs"), (f) => { reportFilter = f; applyReportFilter(); });

loadSkillGaps();
loadNews();
loadReports();
