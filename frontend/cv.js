const fileInput = document.getElementById("cv-file-input");
const dropzone = document.getElementById("dropzone");
const dropzoneText = document.getElementById("dropzone-text");
const uploadBtn = document.getElementById("upload-btn");
const uploadMessage = document.getElementById("upload-message");

function fmtBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function fmtModified(unixSeconds) {
  if (!unixSeconds) return "—";
  return new Date(unixSeconds * 1000).toLocaleString();
}

async function loadCvStatus() {
  const el = document.getElementById("cv-current");
  try {
    const status = await getJSON("/api/cv/status");
    if (!status.has_cv) {
      el.innerHTML = `<p class="empty">No CV uploaded yet — use the form below.</p>`;
      return;
    }

    if (status.parse_error) {
      el.innerHTML = `
        <p><strong>${escapeHtml(status.filename)}</strong> (${fmtBytes(status.size_bytes)}, updated ${fmtModified(status.modified)})</p>
        <p class="save-error">Could not read this file: ${escapeHtml(status.parse_error)}</p>
      `;
      return;
    }

    el.innerHTML = `
      <p><strong>${escapeHtml(status.filename)}</strong> (${fmtBytes(status.size_bytes)}, updated ${fmtModified(status.modified)})</p>
      <p class="card-sub">${status.char_count} characters parsed</p>
      <pre class="cv-preview">${escapeHtml(status.preview)}${status.char_count > 400 ? "…" : ""}</pre>
    `;
  } catch (e) {
    el.innerHTML = `<p class="empty">Could not reach the backend API.</p>`;
  }
}

function setSelectedFile(file) {
  if (!file) return;
  const ok = /\.(pdf|docx)$/i.test(file.name);
  if (!ok) {
    uploadMessage.textContent = "Only .pdf or .docx files are supported.";
    uploadMessage.className = "save-message save-error";
    fileInput.value = "";
    uploadBtn.disabled = true;
    return;
  }
  dropzoneText.textContent = file.name;
  uploadBtn.disabled = false;
  uploadMessage.textContent = "";
  uploadMessage.className = "save-message";
}

fileInput.addEventListener("change", () => setSelectedFile(fileInput.files[0]));

dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dropzone-active");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dropzone-active"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dropzone-active");
  const file = e.dataTransfer.files[0];
  if (file) {
    fileInput.files = e.dataTransfer.files;
    setSelectedFile(file);
  }
});

uploadBtn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  uploadBtn.disabled = true;
  uploadMessage.textContent = "Uploading…";
  uploadMessage.className = "save-message";

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(`${API_BASE}/api/cv/upload`, { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);

    uploadMessage.textContent = "Uploaded.";
    uploadMessage.className = "save-message save-success";
    dropzoneText.textContent = "Drag a file here, or click to choose one";
    fileInput.value = "";
    await loadCvStatus();
  } catch (e) {
    uploadMessage.textContent = e.message || "Upload failed.";
    uploadMessage.className = "save-message save-error";
    uploadBtn.disabled = false;
  }
});

async function loadCvRewrites() {
  const body = document.getElementById("cv-rewrites-body");
  try {
    const rewrites = await getJSON("/api/cv/rewrites?limit=50");
    if (!rewrites.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty">None yet — generated when a job's ATS score is below the fit threshold.</td></tr>`;
      return;
    }
    body.innerHTML = rewrites.map((r) => `
      <tr>
        <td>${escapeHtml(r.job_title || "—")}</td>
        <td>${escapeHtml(r.company || "—")}</td>
        <td>${r.ats_score !== null ? Math.round(r.ats_score * 100) + "%" : "—"}</td>
        <td>${r.date_created ? new Date(r.date_created).toLocaleDateString() : "—"}</td>
        <td><a href="${API_BASE}${r.download_url}" target="_blank" rel="noopener">Download</a></td>
      </tr>
    `).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="5" class="empty">Could not reach the backend API.</td></tr>`;
  }
}

loadCvStatus();
loadCvRewrites();
