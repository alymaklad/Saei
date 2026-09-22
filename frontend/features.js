// Features page: the pipeline stages, the agent roster (each card showing one
// live number from the API), and the whitelist / dry-run state.

const FLOW = [
  { icon: "badge", title: "Canonical profile", sub: "Your source of truth",
    body: "Your CV is extracted once into a structured profile (Profile page) that you can correct by hand. Every score and every tailored CV reads from it — nothing downstream invents facts that aren't there." },
  { icon: "travel_explore", title: "Multi-source search", sub: "Boards, ATS APIs, sites",
    body: "Pulls jobs from Greenhouse and Lever's public APIs, an optional Google Sheets watchlist, sites you add yourself, and SerpAPI's free tier for Google Jobs. Every job is deduped on URL before anything else happens to it." },
  { icon: "hub", title: "Match & rank", sub: "Expansion + embeddings",
    body: "Your Position is expanded into equivalent job titles, then each posting is compared to your CV semantically and ranked on skills, experience, title, location and similarity. A minimum match score can skip weak jobs before any LLM spend." },
  { icon: "fact_check", title: "ATS audit", sub: "Explainable scoring",
    body: "Requirement-by-requirement matching against your profile: each point awarded traces to a line of your CV, and every unmet requirement is listed. That breakdown is what the Why? buttons open." },
  { icon: "edit_document", title: "Truthful tailoring", sub: "Reframe, never invent",
    body: "When the ATS score is below your fit threshold, the CV is rewritten around the posting's real requirements using the posting's own wording — never fabricated. Missing skills are reported as gaps instead of written in." },
  { icon: "shield", title: "Whitelist dispatch", sub: "You stay in control",
    body: "Only sources you've explicitly whitelisted can ever auto-submit. Everything else becomes a complete draft — CV, target URL — held for your review." },
  { icon: "outgoing_mail", title: "Email & reports", sub: "Gmail + Telegram",
    body: "Send application emails from your own Gmail via OAuth, get a daily summary and a weekly news digest on Telegram, and track the skills that cost you the most points." },
];

let activeStep = 1;

function renderFlow() {
  document.getElementById("flow-steps").innerHTML = FLOW.map((s, i) => `
    <button type="button" data-step="${i}" class="flex flex-col rounded-2xl p-3 text-left transition-colors ${i === activeStep ? "bg-surface-container-low" : "hover:bg-surface-container-low/60"}">
      <div class="mb-2 flex items-center justify-between">
        <span class="flex h-6 w-6 items-center justify-center rounded-full text-label-sm font-bold ${i === activeStep ? "bg-primary text-on-primary" : "bg-surface-container-high text-on-surface-variant"}">${i + 1}</span>
        <span class="ms text-[18px] ${i === activeStep ? "text-primary" : "text-on-surface-variant"}">${s.icon}</span>
      </div>
      <span class="text-label-lg text-on-surface">${s.title}</span>
      <span class="text-label-sm text-on-surface-variant">${s.sub}</span>
    </button>`).join("");
  const s = FLOW[activeStep];
  document.getElementById("flow-detail").innerHTML = `
    <div class="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-2xl bg-primary text-on-primary"><span class="ms text-[24px]">${s.icon}</span></div>
    <div>
      <h3 class="text-headline-sm font-bold text-on-surface">Stage ${activeStep + 1}: ${s.title}</h3>
      <p class="mt-1 text-body-md text-on-surface-variant">${s.body}</p>
    </div>`;
}

document.getElementById("flow-steps").addEventListener("click", (e) => {
  const b = e.target.closest("[data-step]");
  if (!b) return;
  activeStep = Number(b.dataset.step);
  renderFlow();
});
renderFlow();

// ---- roster ---------------------------------------------------------------------
// Each agent maps to a real module; `metric` pulls one live number for it from
// the data loaded below, or null when there's nothing to show yet.

const ROSTER = [
  { key: "orchestrator", group: "discovery", name: "The Coordinator", ar: "المرشد", icon: "route", tint: "bg-primary text-on-primary",
    module: "orchestrator.py", role: "Orchestrator",
    desc: "Runs every job through the pipeline graph — score, tailor if needed, decide — and routes it to review or auto-submit.",
    metric: (d) => d.stats && { label: "Jobs processed", value: d.stats.total_applications } },
  { key: "search", group: "discovery", name: "Search Agent", ar: "المستكشف", icon: "radar", tint: "bg-tertiary text-on-tertiary",
    module: "agents/search_agent.py", role: "Reconnaissance",
    desc: "Scans Greenhouse, Lever, Google Jobs, your watchlist and any career site you add, filtering by title, seniority and freshness.",
    metric: (d) => d.stats && { label: "Jobs found", value: d.stats.total_jobs } },
  { key: "expansion", group: "discovery", name: "Query Expansion", ar: "المُوسِّع", icon: "manage_search", tint: "bg-tertiary-fixed text-on-tertiary-fixed",
    module: "agents/query_expansion_agent.py", role: "Vocabulary",
    desc: "Turns one typed Position into the set of titles that describe the same role, so postings worded differently aren't missed.",
    metric: () => null },
  { key: "ranking", group: "discovery", name: "Matching Agent", ar: "المدقق", icon: "hub", tint: "bg-secondary text-on-secondary",
    module: "agents/ranking_agent.py", role: "Vector synapse",
    desc: "Compares each posting to your CV with embeddings and ranks it on skills, experience, title, location and similarity.",
    metric: (d) => d.avgMatch !== null && { label: "Average match", value: `${d.avgMatch}%`, bar: d.avgMatch } },
  { key: "ats", group: "insight", name: "ATS Scoring Agent", ar: "المحلل", icon: "fact_check", tint: "bg-surface-container-highest text-on-surface",
    module: "agents/ats_agent.py", role: "Parser verifier",
    desc: "Line-by-line requirement matching with the evidence for every point awarded — no black-box number.",
    metric: (d) => d.avgAts !== null && { label: "Average ATS (best CV)", value: `${d.avgAts}%`, bar: d.avgAts } },
  { key: "rewriter", group: "discovery", name: "CV Tailoring Agent", ar: "الكاتب", icon: "edit", tint: "bg-primary-container text-on-primary-container",
    module: "agents/cv_rewriter_agent.py", role: "Authentic scribe",
    desc: "Rewrites the CV around a posting's real requirements, in the posting's words, without inventing skills or credentials.",
    metric: (d) => d.tailored !== null && { label: "Tailored CVs", value: d.tailored } },
  { key: "apply", group: "discovery", name: "Apply Agent", ar: "الساعي", icon: "forward_to_inbox", tint: "bg-primary text-on-primary",
    module: "agents/apply_agent.py", role: "Carrier dispatch",
    desc: "Auto-submits only on whitelisted sources (per your auto-apply mode); everything else is drafted for your review.",
    metric: (d) => d.stats && { label: "Awaiting review / auto-submitted", value: `${d.stats.pending_review} / ${d.stats.auto_submitted}` } },
  { key: "email", group: "discovery", name: "Email Agent", ar: "المراسل", icon: "mark_email_read", tint: "bg-secondary text-on-secondary",
    module: "agents/email_agent.py", role: "Diplomat",
    desc: "Sends application emails through your own Gmail account via OAuth — no per-email cost, nothing routed through a third party.",
    metric: (d) => d.stats && { label: "Applications emailed", value: d.stats.emails_sent } },
  { key: "skillgap", group: "insight", name: "Skill Gap Agent", ar: "المدرب", icon: "school", tint: "bg-surface-container-highest text-on-surface",
    module: "agents/skill_gap_agent.py", role: "Growth mentor",
    desc: "Tallies which required skills come up missing most often across recent jobs, so you know what to learn next.",
    metric: (d) => d.topGap && { label: "Most-missed skill", value: d.topGap } },
  { key: "reports", group: "insight", name: "Reports & News Agent", ar: "الإحصائي", icon: "insights", tint: "bg-tertiary text-on-tertiary",
    module: "agents/reporter_agent.py · news_agent.py", role: "Intelligence digest",
    desc: "A daily summary of what happened and a weekly news digest for your field, delivered over a free Telegram bot.",
    metric: (d) => d.reports !== null && { label: "Reports delivered", value: d.reports } },
];

let rosterFilter = "all";
let live = { stats: null, avgMatch: null, avgAts: null, tailored: null, topGap: null, reports: null };

function agentCard(a) {
  const m = a.metric(live);
  return `
    <article class="flex flex-col rounded-3xl bg-surface-container-lowest p-6 shadow-sm transition-shadow hover:shadow-md">
      <div class="mb-4 flex items-start justify-between gap-3">
        <div class="flex items-center gap-3">
          <div class="flex h-12 w-12 items-center justify-center rounded-2xl ${a.tint}"><span class="ms text-[24px]">${a.icon}</span></div>
          <div>
            <h3 class="text-headline-sm font-bold text-on-surface">${a.name}</h3>
            <span class="text-label-sm font-bold text-primary" lang="ar">${a.ar}</span>
          </div>
        </div>
      </div>
      <p class="flex-1 text-body-sm text-on-surface-variant">${a.desc}</p>
      <div class="mt-5 rounded-xl bg-surface-container-low p-3">
        ${m ? `
          <div class="flex items-center justify-between gap-2">
            <span class="text-label-sm text-on-surface-variant">${escapeHtml(m.label)}</span>
            <span class="truncate font-mono text-label-md font-bold text-on-surface">${escapeHtml(String(m.value))}</span>
          </div>
          ${m.bar != null ? `<div class="sa-progress mt-2"><div class="bg-primary" style="width:${m.bar}%"></div></div>` : ""}`
        : `<span class="text-label-sm text-on-surface-variant">No live data yet</span>`}
      </div>
      <div class="mt-4 flex items-center justify-between gap-2 text-label-sm">
        <span class="truncate font-mono text-on-surface-variant" title="${a.module}">${a.module}</span>
        <span class="whitespace-nowrap font-mono font-bold uppercase text-primary">${a.role}</span>
      </div>
    </article>`;
}

function renderRoster() {
  const list = ROSTER.filter((a) => rosterFilter === "all" || a.group === rosterFilter);
  document.getElementById("roster").innerHTML = list.map(agentCard).join("");
  document.getElementById("roster-count").textContent = `${ROSTER.length} specialised agents`;
}

bindTabs(document.getElementById("roster-tabs"), (f) => { rosterFilter = f; renderRoster(); });
renderRoster();

// ---- live data --------------------------------------------------------------

function avgPct(values) {
  const v = values.filter((x) => x !== null && x !== undefined);
  return v.length ? pct(v.reduce((a, b) => a + b, 0) / v.length) : null;
}

async function loadLiveData() {
  const [stats, apps, gaps, reports] = await Promise.allSettled([
    getJSON("/api/stats"),
    getJSON("/api/applications?limit=200"),
    getJSON("/api/skill-gaps?limit=1"),
    getJSON("/api/reports?limit=50"),
  ]);
  if (stats.status === "fulfilled") live.stats = stats.value;
  if (apps.status === "fulfilled") {
    const a = apps.value;
    live.avgMatch = avgPct(a.map((x) => x.match_score));
    live.avgAts = avgPct(a.map((x) => (hasTailored(x) ? x.tailored_ats_score : x.ats_score)));
    live.tailored = a.filter(hasTailored).length;
  }
  if (gaps.status === "fulfilled" && gaps.value.length) live.topGap = gaps.value[0].skill;
  if (reports.status === "fulfilled") live.reports = reports.value.filter((r) => r.status !== "failed").length;
  renderRoster();
}

async function loadLiveFeatureState() {
  const chips = document.getElementById("live-chips");
  try {
    const status = await getJSON("/api/status");

    const whitelistEl = document.getElementById("feature-whitelist");
    whitelistEl.textContent = status.whitelisted_sources.length
      ? `Auto-submit is only allowed on: ${status.whitelisted_sources.join(", ")}. Every other board drafts for review.`
      : "No sources are whitelisted right now — every board drafts for review. Add sources in .env after manually verifying a board's form.";

    const dryRunEl = document.getElementById("feature-dryrun");
    dryRunEl.textContent = status.dry_run
      ? "Currently ON — every send/submit action is logged instead of executed. Set DRY_RUN=false in .env once you trust the setup."
      : "Currently OFF — sends and submissions are live.";

    chips.innerHTML = `
      <span class="${status.dry_run ? "sa-chip-amber" : "sa-chip-teal"}"><span class="ms text-[14px]">${status.dry_run ? "science" : "bolt"}</span>${status.dry_run ? "Dry run on" : "Live mode"}</span>
      <span class="sa-chip"><span class="ms text-[14px]">memory</span>LLM: ${escapeHtml(status.llm_provider)}</span>
      <span class="sa-chip"><span class="ms text-[14px]">verified_user</span>${status.whitelisted_sources.length} whitelisted source${status.whitelisted_sources.length === 1 ? "" : "s"}</span>`;
  } catch (e) {
    document.getElementById("feature-whitelist").textContent = "Could not reach the backend API.";
    document.getElementById("feature-dryrun").textContent = "Could not reach the backend API.";
    chips.innerHTML = `<span class="sa-chip">Backend unreachable</span>`;
  }
}

loadLiveFeatureState();
loadLiveData();
