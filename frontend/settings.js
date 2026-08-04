const providerSelect = document.getElementById("llm-provider");
const ollamaFields = document.getElementById("ollama-fields");
const geminiFields = document.getElementById("gemini-fields");
const geminiKeyStatus = document.getElementById("gemini-key-status");
const saveMessage = document.getElementById("save-message");

function updateVisibleFields() {
  const provider = providerSelect.value;
  ollamaFields.style.display = provider === "ollama" ? "" : "none";
  geminiFields.style.display = provider === "gemini" ? "" : "none";
}

providerSelect.addEventListener("change", updateVisibleFields);

async function loadSettings() {
  try {
    const s = await getJSON("/api/settings");
    providerSelect.value = s.llm_provider;
    document.getElementById("ollama-model").value = s.ollama_model || "";
    document.getElementById("ollama-base-url").value = s.ollama_base_url || "";
    geminiKeyStatus.textContent = s.gemini_api_key_set
      ? "A Gemini API key is currently saved."
      : "No Gemini API key saved yet.";
    updateVisibleFields();
  } catch (e) {
    saveMessage.textContent = "Could not reach the backend API.";
    saveMessage.className = "save-message save-error";
  }
}

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  saveMessage.textContent = "Saving…";
  saveMessage.className = "save-message";

  const payload = {
    llm_provider: providerSelect.value,
    ollama_model: document.getElementById("ollama-model").value || null,
    ollama_base_url: document.getElementById("ollama-base-url").value || null,
    gemini_api_key: document.getElementById("gemini-api-key").value || null,
  };

  try {
    const result = await postJSON("/api/settings", payload);
    saveMessage.textContent = "Saved.";
    saveMessage.className = "save-message save-success";
    document.getElementById("gemini-api-key").value = "";
    geminiKeyStatus.textContent = result.gemini_api_key_set
      ? "A Gemini API key is currently saved."
      : "No Gemini API key saved yet.";
  } catch (err) {
    saveMessage.textContent = err.message || "Could not save settings.";
    saveMessage.className = "save-message save-error";
  }
});

loadSettings();
