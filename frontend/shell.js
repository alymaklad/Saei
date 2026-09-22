// Shared app shell: the Sa'ei sidebar + top bar, injected on every page so the
// nav lives in one place instead of being copy-pasted into nine HTML files.
//
// Each page marks itself with <body data-page="search"> and puts its content
// in <main id="page">. Load this script synchronously at the END of <body>,
// before config.js: it wraps #page in the shell immediately, so the
// #status-pill that config.js fills on DOMContentLoaded already exists.

(function () {
  const NAV = [
    { page: "dashboard", href: "index.html", icon: "grid_view", label: "Dashboard" },
    { page: "search", href: "search.html", icon: "radar", label: "Search" },
    { page: "applications", href: "applications.html", icon: "fact_check", label: "Applications" },
    { page: "cv", href: "cv.html", icon: "description", label: "CV Studio" },
    { page: "profile", href: "profile.html", icon: "badge", label: "Profile" },
    { page: "email", href: "email.html", icon: "send", label: "Email" },
    { page: "reports", href: "reports.html", icon: "trending_up", label: "Reports" },
    { page: "features", href: "features.html", icon: "auto_awesome", label: "Features" },
    { page: "settings", href: "settings.html", icon: "tune", label: "Settings" },
  ];

  const current = document.body.dataset.page || "";
  const currentItem = NAV.find((n) => n.page === current);

  const navHTML = NAV.map((n) => {
    const active = n.page === current;
    const cls = active
      ? "bg-primary text-on-primary font-bold shadow-sm"
      : "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface";
    return `
      <a href="${n.href}" ${active ? 'aria-current="page"' : ""}
         class="flex items-center gap-space-sm rounded-lg px-space-md py-space-sm transition-all ${cls}">
        <span class="ms">${n.icon}</span>
        <span class="text-label-lg">${n.label}</span>
      </a>`;
  }).join("");

  const sidebar = `
    <aside id="sa-sidebar"
      class="fixed left-0 top-0 z-50 flex h-full w-72 flex-col justify-between bg-surface-container-low px-space-md py-space-lg shadow-soft">
      <div class="flex flex-col gap-space-lg overflow-y-auto">
        <a href="index.html" class="flex items-center gap-space-sm px-space-xs">
          <img src="assets/saei-logo.png" alt="" class="h-10 w-10 rounded-xl object-contain">
          <div class="flex flex-col">
            <div class="flex items-baseline gap-space-xs">
              <span class="text-headline-sm font-bold tracking-tight text-on-surface">Sa'ei</span>
              <span class="text-label-md font-bold text-primary" lang="ar">ساعي</span>
            </div>
            <span class="text-label-sm text-on-surface-variant">Your AI Career Companion</span>
          </div>
        </a>
        <nav class="flex flex-col gap-space-xs" aria-label="Main">${navHTML}</nav>
      </div>
      <div class="flex flex-col gap-space-sm pt-space-md">
        <div class="flex items-center justify-between rounded-lg bg-surface-container-high px-space-md py-2">
          <div class="flex items-center gap-2">
            <span class="relative flex h-2 w-2">
              <span id="sa-health-ping" class="absolute inline-flex h-full w-full animate-ping rounded-full bg-outline opacity-75"></span>
              <span id="sa-health-dot" class="relative inline-flex h-2 w-2 rounded-full bg-outline"></span>
            </span>
            <span class="text-label-md text-on-surface">LLM: <span id="sa-llm">&hellip;</span></span>
          </div>
          <span id="sa-health-label" class="text-label-sm font-semibold text-on-surface-variant">Checking</span>
        </div>
        <a href="profile.html" class="flex items-center justify-between rounded-xl bg-surface-container-lowest p-space-sm shadow-soft transition-colors hover:bg-surface-container">
          <div class="flex min-w-0 items-center gap-space-sm">
            <div class="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-primary text-label-md font-bold text-on-primary" id="sa-user-initials">
              <span class="ms text-[18px]">person</span>
            </div>
            <div class="flex min-w-0 flex-col">
              <span class="truncate text-label-lg font-semibold leading-tight text-on-surface" id="sa-user-name">Your profile</span>
              <span class="truncate text-label-sm leading-tight text-on-surface-variant" id="sa-user-title">Set up from your CV</span>
            </div>
          </div>
          <span class="ms text-[18px] text-on-surface-variant">chevron_right</span>
        </a>
      </div>
    </aside>`;

  const header = `
    <header class="fixed left-72 right-0 top-0 z-30 h-16 bg-surface/80 shadow-soft backdrop-blur-xl">
      <div class="flex h-16 w-full items-center justify-between gap-4 px-margin-tablet xl:px-margin-desktop">
        <div class="flex min-w-0 items-center gap-space-sm">
          <span class="truncate text-headline-sm font-semibold text-on-surface">${currentItem ? currentItem.label : "Sa'ei"}</span>
        </div>
        <div class="flex items-center gap-space-md">
          <div id="status-pill" class="pill pill-muted">checking status&hellip;</div>
          <a href="profile.html" class="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-on-primary" aria-label="Profile">
            <span class="ms text-[18px]">person</span>
          </a>
        </div>
      </div>
    </header>`;

  // Wrap the page's own <main id="page"> in the shell.
  const main = document.getElementById("page");
  const wrap = document.createElement("div");
  wrap.className = "pl-72";
  wrap.innerHTML = header;
  main.classList.add("min-h-screen", "w-full", "pb-16", "pt-24", "px-margin-tablet", "xl:px-margin-desktop");
  main.parentNode.insertBefore(wrap, main);
  wrap.appendChild(main);
  document.body.insertAdjacentHTML("afterbegin", sidebar);

  // ---- sidebar user card ----
  // /api/profile re-parses the CV file on every call, so the name/title is
  // cached for the browser session rather than fetched on every page view.
  // profile.js clears this key after a save so a rename shows up at once.
  const USER_KEY = "saei.sidebarUser";
  function paintUser(u) {
    if (!u || !u.name) return;
    document.getElementById("sa-user-name").textContent = u.name;
    document.getElementById("sa-user-title").textContent = u.title || "Profile";
    const initials = u.name.trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
    document.getElementById("sa-user-initials").textContent = initials;
    // Pages that greet the user by name (the Dashboard) listen for this.
    window.saeiUser = u;
    document.dispatchEvent(new CustomEvent("saei:user", { detail: u }));
  }
  let cached = null;
  try { cached = JSON.parse(sessionStorage.getItem(USER_KEY) || "null"); } catch (e) { cached = null; }
  if (cached) paintUser(cached);

  window.addEventListener("DOMContentLoaded", async () => {
    if (typeof getJSON !== "function") return;
    try {
      const s = await getJSON("/api/status");
      document.getElementById("sa-llm").textContent = s.llm_provider;
      const live = !s.dry_run;
      document.getElementById("sa-health-label").textContent = live ? "Live" : "Dry run";
      document.getElementById("sa-health-label").className =
        "text-label-sm font-semibold " + (live ? "text-secondary" : "text-tertiary");
      ["sa-health-ping", "sa-health-dot"].forEach((id) => {
        const el = document.getElementById(id);
        el.classList.remove("bg-outline");
        el.classList.add(live ? "bg-secondary" : "bg-tertiary-fixed-dim");
      });
    } catch (e) {
      document.getElementById("sa-llm").textContent = "offline";
      document.getElementById("sa-health-label").textContent = "Offline";
      document.getElementById("sa-health-ping").classList.remove("animate-ping");
    }
    if (cached) return;
    try {
      const p = await getJSON("/api/profile");
      const c = (p.profile && p.profile.contact) || {};
      const u = { name: c.full_name || "", title: c.title || "" };
      if (u.name) {
        try { sessionStorage.setItem(USER_KEY, JSON.stringify(u)); } catch (e) { /* private mode */ }
        paintUser(u);
      }
    } catch (e) { /* card keeps its placeholder */ }
  });
})();
