async function loadLiveFeatureState() {
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
  } catch (e) {
    document.getElementById("feature-whitelist").textContent = "Could not reach the backend API.";
    document.getElementById("feature-dryrun").textContent = "Could not reach the backend API.";
  }
}

loadLiveFeatureState();
