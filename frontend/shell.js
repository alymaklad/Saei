// Shared app shell: the Sa'ei sidebar + top bar, injected on every page so the
// nav lives in one place instead of being copy-pasted into every HTML file.
//
// Each page marks itself with <body data-page="search"> and puts its content
// in <main id="page">. Load this script synchronously at the END of <body>,
// before config.js: it wraps #page in the shell immediately, so the
// #status-pill that config.js fills on DOMContentLoaded already exists.

(function () {
  // The 2026-09-24 design has seven destinations. Email and Features were
  // folded in elsewhere: sending lives on Applications, the Gmail connection
  // on Settings, and the sent-email log on Reports.
  const NAV = [
    { page: "dashboard", href: "index.html", icon: "home", label: "Home" },
    { page: "search", href: "search.html", icon: "explore", label: "Search" },
    { page: "applications", href: "applications.html", icon: "task_alt", label: "Applications" },
    { page: "cv", href: "cv.html", icon: "description", label: "CV" },
    { page: "profile", href: "profile.html", icon: "person", label: "Profile" },
    { page: "reports", href: "reports.html", icon: "insights", label: "Reports" },
    { page: "settings", href: "settings.html", icon: "tune", label: "Settings" },
  ];

  const current = document.body.dataset.page || "";
  const currentItem = NAV.find((n) => n.page === current);

  const navHTML = NAV.map((n) => {
    const active = n.page === current;
    const cls = active
      ? "bg-secondary-container text-on-secondary-fixed font-semibold"
      : "text-on-surface-variant hover:bg-surface-container hover:text-on-surface";
    return `
      <a href="${n.href}" ${active ? 'aria-current="page"' : ""}
         class="flex items-center gap-space-sm rounded-lg px-space-sm py-space-sm transition-colors ${cls}">
        <span class="ms">${n.icon}</span>
        <span class="text-label-md">${n.label}</span>
      </a>`;
  }).join("");

  const sidebar = `
    <aside id="sa-sidebar"
      class="fixed left-0 top-0 z-50 flex h-screen w-72 flex-col justify-between bg-surface-container-low/70 p-space-md shadow-soft backdrop-blur-xl">
      <div class="flex flex-col gap-space-lg overflow-y-auto">
        <a href="index.html" class="flex items-center gap-space-sm px-space-xs pt-space-sm">
          <img src="assets/saei-logo.png" alt="" class="h-11 w-11 rounded-xl object-contain">
          <div class="flex flex-col">
            <div class="flex items-baseline gap-space-xs">
              <span class="text-headline-sm tracking-tight text-on-surface">Sa'ei</span>
              <span class="text-label-md text-primary" lang="ar">ساعي</span>
            </div>
            <span class="text-label-sm uppercase tracking-widest text-on-surface-variant">Career Companion</span>
          </div>
        </a>
        <nav class="flex flex-col gap-space-xs" aria-label="Main">${navHTML}</nav>
      </div>
      <a href="profile.html" class="flex items-center justify-between gap-space-sm rounded-lg bg-surface-container-lowest/80 p-space-sm shadow-soft transition-colors hover:bg-surface-container-lowest">
        <div class="flex min-w-0 items-center gap-space-sm">
          <div class="relative flex-shrink-0">
            <div class="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-label-sm text-on-primary" id="sa-user-initials">
              <span class="ms text-[18px]">person</span>
            </div>
            <span id="sa-health-dot" title="Checking backend…"
                  class="absolute bottom-0 right-0 h-2 w-2 rounded-full bg-outline ring-1 ring-surface-container-lowest"></span>
          </div>
          <div class="flex min-w-0 flex-col">
            <span class="truncate text-label-md font-semibold text-on-surface" id="sa-user-name">Your profile</span>
            <span class="truncate text-label-sm normal-case tracking-normal text-on-surface-variant" id="sa-user-title">Set up from your CV</span>
          </div>
        </div>
        <span class="ms text-[18px] text-on-surface-variant">unfold_more</span>
      </a>
    </aside>`;

  const header = `
    <header class="fixed left-72 right-0 top-0 z-40 h-16 bg-background/80 shadow-soft backdrop-blur-xl">
      <div class="flex h-16 w-full items-center justify-between px-space-lg">
        <div class="flex items-center gap-space-sm text-on-surface-variant">
          <span class="text-label-sm uppercase tracking-wider text-secondary">Workspace</span>
          <span class="text-body-sm text-outline-variant">/</span>
          <span class="text-label-md font-medium text-on-surface">${currentItem ? currentItem.label : "Sa'ei"}</span>
        </div>
        <div class="flex items-center gap-space-md">
          <div id="status-pill" class="pill pill-muted">checking status&hellip;</div>
          <a href="profile.html" class="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-on-primary" aria-label="Profile">
            <span class="ms text-[18px]">person</span>
          </a>
        </div>
      </div>
    </header>`;

  // Wrap the page's own <main id="page"> in the shell: a centred 72rem column
  // under a fixed 4rem header, as in the design.
  const main = document.getElementById("page");
  const wrap = document.createElement("div");
  wrap.className = "pl-72";
  wrap.innerHTML = header;
  const column = document.createElement("div");
  column.className = "min-h-screen w-full bg-background pt-16";
  main.classList.add("mx-auto", "max-w-6xl", "px-space-lg", "py-space-xl");
  main.parentNode.insertBefore(wrap, main);
  wrap.appendChild(column);
  column.appendChild(main);

  // Page footer: the landscape mark and the caravan quote on every page, plus
  // an optional page-specific note from <body data-footer="...">.
  const note = document.body.dataset.footer || "";
  main.insertAdjacentHTML("beforeend", `
    <footer class="mt-space-xl flex items-center justify-between gap-space-md border-t border-surface-container pt-space-md text-body-sm text-on-surface-variant">
      <span class="flex items-center gap-space-sm">
        <span class="ms text-[18px] text-primary" aria-hidden="true">landscape</span>
        <span class="italic">“Big dreams aren't reached in a single stride — the caravan advances step by deliberate step.”</span>
      </span>
      ${note ? `<span class="text-right">${note.replace(/</g, "&lt;")}</span>` : ""}
    </footer>`);
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
    // Pages that greet the user by name (Home) listen for this.
    window.saeiUser = u;
    document.dispatchEvent(new CustomEvent("saei:user", { detail: u }));
  }
  let cached = null;
  try { cached = JSON.parse(sessionStorage.getItem(USER_KEY) || "null"); } catch (e) { cached = null; }
  if (cached) paintUser(cached);

  window.addEventListener("DOMContentLoaded", async () => {
    if (typeof getJSON !== "function") return;
    const dot = document.getElementById("sa-health-dot");
    try {
      const s = await getJSON("/api/status");
      dot.classList.remove("bg-outline");
      dot.classList.add(s.dry_run ? "bg-tertiary-fixed-dim" : "bg-tertiary-container");
      dot.title = `Backend online · LLM: ${s.llm_provider}${s.dry_run ? " · dry run" : ""}`;
    } catch (e) {
      dot.classList.replace("bg-outline", "bg-error");
      dot.title = "Backend unreachable";
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
