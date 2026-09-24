// Profile page chrome: the identity card, the one-line summary under each
// section, and the one-section-open-at-a-time accordion. Reads profile.js's
// `state` (loaded first) and never writes to it -- editing stays in profile.js.

(function () {
  const sections = [...document.querySelectorAll(".acc")];

  function open(key) {
    sections.forEach((s) => {
      const isOpen = s.dataset.acc === key && !s.classList.contains("is-open");
      s.classList.toggle("is-open", isOpen);
      s.querySelector(".acc-toggle").textContent = isOpen ? "Collapse" : "Expand";
    });
  }

  sections.forEach((s) => {
    s.querySelector(".acc-toggle").textContent = "Expand";
    s.querySelector(".acc-head").addEventListener("click", () => open(s.dataset.acc));
  });

  // An "Add entry" button inside a closed section can't be clicked, but one
  // clicked from elsewhere (none today) should still reveal its section.
  document.querySelectorAll("[data-add]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sec = btn.closest(".acc");
      if (sec && !sec.classList.contains("is-open")) open(sec.dataset.acc);
    });
  });

  function first(list, fn, n = 3) {
    return (list || []).slice(0, n).map(fn).filter(Boolean).join(", ");
  }

  function link(value, icon) {
    const v = String(value || "").trim();
    if (!v) return "";
    const href = /^https?:\/\//i.test(v) ? v : `https://${v}`;
    const label = v.replace(/^https?:\/\/(www\.)?/i, "").replace(/\/$/, "");
    return `<a class="flex items-center gap-1 font-mono hover:text-primary" href="${escapeHtml(href)}" target="_blank" rel="noopener">
      <span class="ms text-[16px]">${icon}</span>${escapeHtml(label)}</a>`;
  }

  window.renderProfileChrome = function () {
    if (typeof state === "undefined" || !state.profile) return;
    const p = state.profile;
    const c = p.contact || {};

    const name = (c.full_name || "").trim();
    document.getElementById("id-name").textContent = name || "Your name";
    document.getElementById("id-initials").textContent =
      name ? name.split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase() : "—";
    document.getElementById("id-title").textContent = c.title || "";
    const loc = document.getElementById("id-location");
    loc.innerHTML = c.location ? `<span class="ms text-[14px]">location_on</span>${escapeHtml(c.location)}` : "";
    loc.classList.toggle("hidden", !c.location);
    loc.classList.toggle("inline-flex", !!c.location);
    const summary = (p.summary || "").trim();
    document.getElementById("id-summary").textContent = summary
      ? `“${summary.length > 220 ? summary.slice(0, 220).replace(/\s+\S*$/, "") + "…" : summary}”` : "";
    document.getElementById("id-links").innerHTML =
      [link(c.github, "code"), link(c.linkedin, "work")].filter(Boolean).join('<span class="text-primary-fixed-dim">•</span>');

    const sub = {
      contact: [c.email && `Email: ${c.email}`, c.phone && `Phone: ${c.phone}`].filter(Boolean).join(" · ") || "Add your contact details",
      experience: (p.experience || []).length
        ? first(p.experience, (e) => [e.title, e.organization].filter(Boolean).join(" at "))
        : "No roles yet",
      projects: (p.projects || []).length ? first(p.projects, (x) => x.name) : "No projects yet",
      education: (p.education || []).length
        ? first(p.education, (e) => [[e.degree, e.field].filter(Boolean).join(" in "), e.institution].filter(Boolean).join(" · "), 2)
        : "No education yet",
    };
    Object.entries(sub).forEach(([key, text]) => {
      const el = document.querySelector(`[data-acc-sub="${key}"]`);
      if (el) el.textContent = text;
    });
  };

  // profile.js has already started loading; paint whatever is there, and
  // markDirty() (called at the end of every load and edit) repaints after.
  window.renderProfileChrome();
  open("skills");
})();
