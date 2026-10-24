/* ============================================================
   DatLens_OCR — frontend logic (vanilla JS, no build step)
   ============================================================ */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const ACCEPTED_EXT = new Set([".pdf", ".docx", ".html", ".htm", ".png", ".jpg", ".jpeg", ".webp"]);

const state = {
  theme: localStorage.getItem("datlens-theme") || "dark",
  sourceTab: "upload",
  engine: "ollama", // single-engine build
  files: [],
  activeJobId: null,
  finished: false, // active job already rendered in its terminal state
  es: null,
  elapsedTimer: null,
  pollTimer: null,
  startedAt: Date.now(),
  resultCache: {}, // fmt -> text
  currentFmt: "markdown",
  pages: null, // {total, pages:[...]}
  pageIndex: 0,
};

/* ---------------- theme ---------------- */

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  $("#icon-moon").classList.toggle("hidden", state.theme !== "dark");
  $("#icon-sun").classList.toggle("hidden", state.theme === "dark");
}
$("#theme-toggle").addEventListener("click", () => {
  state.theme = state.theme === "dark" ? "light" : "dark";
  localStorage.setItem("datlens-theme", state.theme);
  applyTheme();
});

/* ---------------- helpers ---------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
    } catch { /* not json */ }
    throw new Error(detail);
  }
  return res;
}
const apiJSON = async (path, opts) => (await api(path, opts)).json();

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function toast(msg, kind = "err") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => t.remove(), 350);
  }, 5600);
}

function downloadBlob(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/* ---------------- config + ollama panel ---------------- */

async function initConfig() {
  try {
    const cfg = await apiJSON("/api/config");
    const sel = $("#dpi-select");
    for (const dpi of cfg.dpi_options) {
      const o = document.createElement("option");
      o.value = String(dpi);
      o.textContent = `${dpi} DPI`;
      if (dpi === cfg.default_dpi) o.selected = true;
      sel.appendChild(o);
    }
    $("#ollama-url").value = localStorage.getItem("datlens-ollama-url") || cfg.default_ollama_url;
  } catch (e) {
    console.error("config failed", e);
  }
  refreshModels();
}

async function refreshModels() {
  const url = $("#ollama-url").value.trim() || "http://localhost:11434";
  const statusEl = $("#ollama-status");
  const chip = $("#ollama-chip");
  statusEl.textContent = "Contacting server…";
  try {
    const data = await apiJSON(`/api/ollama/models?base_url=${encodeURIComponent(url)}`);
    const sel = $("#ollama-model");
    const prev = sel.value;
    sel.innerHTML = "";
    if (data.reachable) {
      chip.className = "chip ok";
      chip.classList.remove("hidden");
      chip.innerHTML = `<span class="dot"></span>Ollama · ${data.models.length} model${data.models.length === 1 ? "" : "s"}`;
      statusEl.textContent = `Connected — ${data.models.length} model(s) available.`;
      if (!data.models.length) {
        addOption(sel, "(no models found — pull one first)", "");
      } else {
        for (const m of data.models) addOption(sel, m, m);
      }
      if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
    } else {
      chip.className = "chip err";
      chip.classList.remove("hidden");
      chip.innerHTML = `<span class="dot"></span>Ollama unreachable`;
      statusEl.textContent = `Cannot reach ${data.base_url}. The Ollama engine needs a reachable server.`;
      addOption(sel, "(server unreachable)", "");
    }
    localStorage.setItem("datlens-ollama-url", url);
  } catch (e) {
    chip.className = "chip err";
    chip.classList.remove("hidden");
    chip.innerHTML = `<span class="dot"></span>Ollama error`;
    statusEl.textContent = `Model lookup failed: ${e.message}`;
  }
}

function addOption(sel, label, value) {
  const o = document.createElement("option");
  o.textContent = label;
  o.value = value;
  sel.appendChild(o);
}

/* ---------------- source panel ---------------- */

$$("#source-tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    state.sourceTab = b.dataset.tab;
    $$("#source-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $("#tab-upload").classList.toggle("hidden", state.sourceTab !== "upload");
    $("#tab-url").classList.toggle("hidden", state.sourceTab !== "url");
  })
);

const dz = $("#dropzone");
const fi = $("#file-input");
dz.addEventListener("click", () => fi.click());
dz.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fi.click();
  }
});
fi.addEventListener("change", () => {
  addFiles(fi.files);
  fi.value = "";
});
["dragenter", "dragover"].forEach((ev) =>
  dz.addEventListener(ev, (e) => {
    e.preventDefault();
    dz.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
  })
);
dz.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));

function addFiles(fileList) {
  for (const f of fileList) {
    const ext = "." + (f.name.split(".").pop() || "").toLowerCase();
    if (!ACCEPTED_EXT.has(ext)) {
      toast(`Skipped ${f.name} — unsupported type (${ext})`);
      continue;
    }
    state.files.push(f);
  }
  renderFileList();
}

function renderFileList() {
  const ul = $("#file-list");
  ul.innerHTML = "";
  state.files.forEach((f, i) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "name ellipsis";
    name.textContent = f.name;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = fmtBytes(f.size);
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.title = "Remove";
    rm.addEventListener("click", () => {
      state.files.splice(i, 1);
      renderFileList();
    });
    li.append(name, size, rm);
    ul.appendChild(li);
  });
}

/* ---------------- start ---------------- */

$("#start-btn").addEventListener("click", async () => {
  const opts = {
    engine: state.engine,
    base_url: $("#ollama-url").value.trim() || "http://localhost:11434",
    model: $("#ollama-model").value || "",
    dpi: Number($("#dpi-select").value) || 150,
  };
  try {
    let jobs;
    if (state.sourceTab === "upload") {
      if (!state.files.length) return toast("Add files first (drop or browse).");
      const fd = new FormData();
      state.files.forEach((f) => fd.append("files", f));
      Object.entries(opts).forEach(([k, v]) => fd.append(k, v));
      jobs = (await apiJSON("/api/jobs/upload", { method: "POST", body: fd })).jobs;
    } else {
      const urls = $("#url-input").value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!urls.length) return toast("Enter at least one document URL first.");
      jobs = [];
      for (const url of urls) {
        try {
          const data = await apiJSON("/api/jobs/url", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, ...opts }),
          });
          jobs.push({ job_id: data.job_id, name: data.name, engine: data.engine });
        } catch (e) {
          toast(`Could not fetch ${url}: ${e.message}`);
        }
      }
      if (!jobs.length) return;
    }
    state.startedAt = Date.now();
    refreshJobList();
    if (jobs.length) selectJob(jobs[jobs.length - 1].job_id);
  } catch (e) {
    toast(`Could not start job: ${e.message}`);
  }
});

$("#refresh-models").addEventListener("click", refreshModels);
$("#ollama-url").addEventListener("change", refreshModels);

/* ---------------- jobs list ---------------- */

async function refreshJobList() {
  try {
    const data = await apiJSON("/api/jobs");
    const ul = $("#job-list");
    ul.innerHTML = "";
    $("#job-list-empty").classList.toggle("hidden", data.jobs.length > 0);
    for (const j of data.jobs) {
      const li = document.createElement("li");
      li.dataset.id = j.job_id;
      li.className = j.status + (j.job_id === state.activeJobId ? " active" : "");
      const name = document.createElement("span");
      name.className = "jname ellipsis";
      name.textContent = j.source || j.job_id;
      const eng = document.createElement("span");
      eng.className = "jengine";
      eng.textContent = j.engine;
      const rm = document.createElement("button");
      rm.textContent = "✕";
      const active = j.status === "queued" || j.status === "running";
      rm.title = active ? "Cancel and remove job" : "Delete job";
      rm.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (active && !confirm(`Stop and remove "${j.source || j.job_id}"?`)) return;
        try {
          await api(`/api/jobs/${j.job_id}`, { method: "DELETE" });
        } catch { /* gone */ }
        if (state.activeJobId === j.job_id) resetWorkspace();
        refreshJobList();
      });
      li.append(dotSpan(j.status), name, eng, rm);
      li.addEventListener("click", () => selectJob(j.job_id));
      ul.appendChild(li);
    }
  } catch { /* server briefly unavailable */ }
}

function dotSpan(status) {
  const s = document.createElement("span");
  s.className = "dot";
  return s;
}

/* ---------------- workspace / job lifecycle ---------------- */

function resetWorkspace() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  stopElapsed();
  state.activeJobId = null;
  state.finished = false;
  $("#work-area").classList.add("hidden");
  $("#empty-state").classList.remove("hidden");
}

function selectJob(id) {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  stopElapsed();
  state.activeJobId = id;
  state.finished = false;
  state.resultCache = {};
  state.currentFmt = "markdown";
  state.pages = null;
  $$("#job-list li").forEach((li) => li.classList.toggle("active", li.dataset.id === id));

  state.pageIndex = 0;
  $$("#result-tabs button").forEach((x) => x.classList.toggle("active", x.dataset.fmt === "markdown"));
  $("#review-single").classList.add("hidden");
  $("#page-text").textContent = "";
  $("#review-empty").classList.add("hidden");
  $("#empty-state").classList.add("hidden");
  $("#work-area").classList.remove("hidden");
  $("#results-card").classList.add("hidden");
  $("#live-card").classList.add("hidden");
  const logEl = $("#log-lines");
  logEl.classList.add("hidden");
  logEl.textContent = "";
  const fill = $("#progress-fill");
  fill.classList.add("indeterminate");
  fill.style.width = "0%";

  apiJSON(`/api/jobs/${id}`)
    .then((job) => {
      if (state.activeJobId !== id) return; // user switched jobs meanwhile
      renderSnapshot(job);
      if (job.status === "done") finishActive("done", job);
      else if (job.status === "cancelled") finishActive("cancelled", job);
      else if (job.status === "error") finishActive("error", job);
      else openStream(id, job.created_at);
    })
    .catch((e) => toast(`Failed to load job: ${e.message}`));
}

function renderSnapshot(job) {
  $("#job-title").textContent = job.source || job.job_id;
  const d = job.detail || {};
  const bits = [job.engine];
  if (d.model) bits.push(d.model);
  if (d.dpi) bits.push(`${d.dpi} DPI`);
  bits.push(fmtTime(job.created_at));
  $("#job-meta").textContent = bits.join(" · ");
  setBadge(job.status);
  updateProgressFromSnapshot(job);
  if (job.phase) $("#phase-label").textContent = job.phase;
  if (job.error) appendLog(`ERROR: ${job.error}`);
}

function updateProgressFromSnapshot(job) {
  const fill = $("#progress-fill");
  if (typeof job.total_pages === "number" && job.total_pages > 0 && job.current_page > 0) {
    fill.classList.remove("indeterminate");
    fill.style.width = `${Math.round((100 * job.current_page) / job.total_pages)}%`;
    $("#progress-label").textContent = `page ${job.current_page}/${job.total_pages}`;
  } else if (job.status === "done") {
    fill.classList.remove("indeterminate");
    fill.style.width = "100%";
    $("#progress-label").textContent = "";
  }
}

function setBadge(status) {
  const b = $("#job-status-badge");
  b.textContent = status;
  b.className = `badge ${status}`;
  const cancelBtn = $("#cancel-btn");
  if (status === "queued" || status === "running") {
    cancelBtn.classList.remove("hidden");
  } else {
    cancelBtn.classList.add("hidden");
    cancelBtn.disabled = false;
    cancelBtn.textContent = "Cancel";
  }
}

$("#cancel-btn").addEventListener("click", async () => {
  if (!state.activeJobId) return;
  const btn = $("#cancel-btn");
  btn.disabled = true;
  btn.textContent = "Cancelling…";
  try {
    await api(`/api/jobs/${state.activeJobId}/cancel`, { method: "POST" });
  } catch (e) {
    toast(`Could not cancel job: ${e.message}`);
    btn.disabled = false;
    btn.textContent = "Cancel";
  }
});

function openStream(id, createdAt) {
  const es = new EventSource(`/api/jobs/${id}/events`);
  state.es = es;

  es.addEventListener("status", (ev) => {
    if (state.activeJobId !== id) return;
    const d = JSON.parse(ev.data);
    if (d.status === "done") return finishActive("done");
    if (d.status === "cancelled") {
      appendLog("Job cancelled.");
      return finishActive("cancelled");
    }
    if (d.status === "error") {
      if (d.error) appendLog(`ERROR: ${d.error}`);
      return finishActive("error");
    }
    setBadge("running");
    if (d.phase) $("#phase-label").textContent = d.phase;
    if (typeof d.page === "number" && typeof d.total === "number" && d.total > 0) {
      const fill = $("#progress-fill");
      fill.classList.remove("indeterminate");
      fill.style.width = `${Math.round((100 * d.page) / d.total)}%`;
      $("#progress-label").textContent = `page ${d.page}/${d.total}`;
    }
  });

  es.addEventListener("log", (ev) => {
    if (state.activeJobId !== id) return;
    appendLog(JSON.parse(ev.data).message || "");
  });

  es.addEventListener("page_image", (ev) => {
    if (state.activeJobId !== id) return;
    const d = JSON.parse(ev.data);
    $("#live-card").classList.remove("hidden");
    $("#live-image").src = `data:image/png;base64,${d.image_b64}`;
    $("#live-page-label").textContent = `Page ${d.page}/${d.total}`;
    $("#live-text").textContent = "";
  });

  es.addEventListener("stream_chunk", (ev) => {
    if (state.activeJobId !== id) return;
    const d = JSON.parse(ev.data);
    const el = $("#live-text");
    el.textContent += d.text || "";
    el.scrollTop = el.scrollHeight;
  });

  es.addEventListener("done", (ev) => {
    if (state.activeJobId !== id) return;
    try {
      const d = JSON.parse(ev.data);
      if (d.warnings && d.warnings.length) d.warnings.forEach((w) => appendLog(`WARN: ${w}`));
      if (d.processing_time != null) appendLog(`Finished in ${d.processing_time}s`);
    } catch { /* ignore */ }
    finishActive("done");
  });

  es.onerror = () => {
    // Transport hiccup — EventSource reconnects automatically; the polling
    // fallback below guarantees we still notice terminal states.
  };

  startElapsed(createdAt ? createdAt * 1000 : Date.now());
}

function finishActive(status, job) {
  // A terminal job reaches here from SSE *and* the 5s poller. Render exactly
  // once — re-running it would force-refetch the result and reset the review
  // view (tab + page index) on every poll cycle.
  if (!state.activeJobId || state.finished) return;
  state.finished = true;
  stopElapsed();
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  setBadge(status);
  const fill = $("#progress-fill");
  fill.classList.remove("indeterminate");
  // The scan is over — drop the live view; the results card (with the
  // per-page review) replaces it.
  $("#live-card").classList.add("hidden");
  $("#live-image").removeAttribute("src");
  $("#live-text").textContent = "";
  $("#live-page-label").textContent = "";
  if (status === "done") {
    fill.style.width = "100%";
    $("#phase-label").textContent = job && job.processing_time != null ? `Done in ${job.processing_time}s` : "Complete";
    $("#results-card").classList.remove("hidden");
    const id = state.activeJobId;
    loadPages(id).then((hasPages) => {
      if (state.activeJobId !== id || !state.finished) return; // switched jobs meanwhile
      if (hasPages) {
        setActiveTab("pages");
        loadResult("pages");
      } else {
        setActiveTab("markdown");
        loadResult("markdown", true);
      }
    });
  } else {
    fill.style.width = "100%";
    $("#phase-label").textContent = status === "cancelled" ? "Cancelled" : "Failed";
  }
  refreshJobList();
}

/* ---------------- elapsed timer + polling fallback ---------------- */

function startElapsed(startMs) {
  stopElapsed();
  state.startedAt = startMs;
  tickElapsed();
  state.elapsedTimer = setInterval(tickElapsed, 1000);
}
function tickElapsed() {
  $("#elapsed").textContent = fmtElapsed(Date.now() - state.startedAt);
}
function stopElapsed() {
  if (state.elapsedTimer) {
    clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
  }
}

// Global poller: catches terminal states even if the SSE stream is flaky.
state.pollTimer = setInterval(async () => {
  if (!state.activeJobId || state.finished || $("#work-area").classList.contains("hidden")) return;
  try {
    const job = await apiJSON(`/api/jobs/${state.activeJobId}`);
    if (job.status === "done") finishActive("done", job);
    else if (job.status === "cancelled") finishActive("cancelled", job);
    else if (job.status === "error") finishActive("error", job);
    else renderSnapshot(job);
  } catch { /* ignore */ }
}, 5000);

/* ---------------- log / live preview ---------------- */

function appendLog(msg) {
  const el = $("#log-lines");
  if (!msg) return;
  const lines = el.textContent ? el.textContent.split("\n") : [];
  if (lines[lines.length - 1] === msg) return; // dedupe replay/live overlap
  lines.push(msg);
  while (lines.length > 300) lines.shift();
  el.textContent = lines.join("\n");
  el.classList.remove("hidden");
  el.scrollTop = el.scrollHeight;
}

/* ---------------- results ---------------- */

function setActiveTab(fmt) {
  $$("#result-tabs button").forEach((x) => x.classList.toggle("active", x.dataset.fmt === fmt));
}

$$("#result-tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    setActiveTab(b.dataset.fmt);
    loadResult(b.dataset.fmt);
  })
);

// Fetch (and cache) a result format's text without changing the visible tab —
// used by Copy/Download while the Review view is on screen.
async function ensureText(fmt) {
  if (state.resultCache[fmt] != null) return state.resultCache[fmt];
  const res = await api(`/api/jobs/${state.activeJobId}/result?format=${fmt}`);
  const text = await res.text();
  state.resultCache[fmt] = text;
  return text;
}

async function loadResult(fmt, force = false) {
  if (!state.activeJobId) return;
  state.currentFmt = fmt;
  const out = $("#result-md");
  const frame = $("#result-html");
  const pagesView = $("#pages-view");
  out.classList.add("hidden");
  frame.classList.add("hidden");
  pagesView.classList.add("hidden");

  if (fmt === "pages") {
    pagesView.classList.remove("hidden");
    renderReview();
    return;
  }
  try {
    let text = state.resultCache[fmt];
    if (text == null || force) text = await ensureText(fmt);
    if (state.currentFmt !== fmt) return; // user switched tabs meanwhile
    if (fmt === "html") {
      frame.srcdoc = text;
      frame.classList.remove("hidden");
    } else {
      out.textContent = text;
      out.classList.remove("hidden");
    }
  } catch (e) {
    toast(`Could not load ${fmt} result: ${e.message}`);
  }
}

async function loadPages(id) {
  try {
    const data = await apiJSON(`/api/jobs/${id}/pages`);
    if (state.activeJobId !== id) return false; // user switched jobs meanwhile
    state.pages = data;
    state.pageIndex = 0;
    renderReview();
    return !!(data && data.pages && data.pages.length);
  } catch { /* pages optional */ }
  return false;
}

// Post-scan review: one page at a time — the original document image next to
// its extracted text, with Previous/Next buttons to walk through the pages.
function renderReview() {
  const single = $("#review-single");
  if (!state.pages || !state.pages.pages.length) {
    single.classList.add("hidden");
    $("#review-empty").classList.remove("hidden");
    return;
  }
  $("#review-empty").classList.add("hidden");
  single.classList.remove("hidden");
  state.pageIndex = Math.min(state.pageIndex, state.pages.pages.length - 1);
  const p = state.pages.pages[state.pageIndex];
  $("#page-label").textContent = `Page ${p.page} of ${state.pages.total}`;
  const img = $("#page-image");
  if (p.image_b64) {
    img.src = `data:image/png;base64,${p.image_b64}`;
    img.classList.remove("hidden");
  } else {
    img.removeAttribute("src");
    img.classList.add("hidden");
  }
  $("#page-text").textContent = p.text || "(no text for this page)";
  $("#page-prev").disabled = state.pageIndex === 0;
  $("#page-next").disabled = state.pageIndex === state.pages.pages.length - 1;
}

$("#page-prev").addEventListener("click", () => {
  if (state.pageIndex > 0) {
    state.pageIndex -= 1;
    renderReview();
  }
});
$("#page-next").addEventListener("click", () => {
  if (state.pages && state.pageIndex < state.pages.pages.length - 1) {
    state.pageIndex += 1;
    renderReview();
  }
});

/* ---------------- copy / download ---------------- */

const FMT_EXT = { markdown: "md", html: "html", json: "json", text: "txt" };

// While the Review view is on screen, Copy/Download act on the markdown.
const copyDownloadFmt = () => (state.currentFmt === "pages" ? "markdown" : state.currentFmt);

$("#copy-btn").addEventListener("click", async () => {
  if (!state.activeJobId) return;
  const fmt = copyDownloadFmt();
  try {
    await navigator.clipboard.writeText(await ensureText(fmt));
    toast("Copied to clipboard", "ok");
  } catch (e) {
    toast(e.message.includes("clipboard") || e.name === "NotAllowedError"
      ? "Clipboard unavailable in this browser context."
      : `Copy failed: ${e.message}`);
  }
});

$("#download-btn").addEventListener("click", async () => {
  if (!state.activeJobId) return;
  const fmt = copyDownloadFmt();
  let text;
  try {
    text = await ensureText(fmt);
  } catch (e) {
    return toast(`Could not load ${fmt} result: ${e.message}`);
  }
  let base = "document";
  try {
    const job = $("#job-title").textContent || "document";
    base = job.replace(/\.[a-z0-9]+$/i, "").slice(0, 80) || "document";
  } catch { /* default */ }
  downloadBlob(new Blob([text], { type: "text/plain;charset=utf-8" }), `${base}.${FMT_EXT[fmt]}`);
});

/* ---------------- boot ---------------- */

applyTheme();
initConfig();
refreshJobList();
