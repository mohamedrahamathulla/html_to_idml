/* html2idml web UI - vanilla JS, no external dependencies */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const fmtSize = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n > 1024 ? (n / 1024).toFixed(0) + " KB" : n + " B";
const ago = (t) => { const s = Date.now() / 1000 - t; if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago"; return new Date(t * 1000).toLocaleDateString(); };
const round = (v, n = 2) => (typeof v === "number" ? Math.round(v * 10 ** n) / 10 ** n : v);

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 2800);
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* not json */ }
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

/* ---------------- theme ---------------- */
const applyTheme = (t) => { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; };
applyTheme(store.get("theme", null));
$("#themeBtn").onclick = () => {
  const cur = document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark"; applyTheme(next); store.set("theme", next);
};

/* ---------------- meta ---------------- */
let META = { installed_fonts: [] };
api("/api/meta").then(m => {
  META = m;
  const dl = document.createElement("datalist"); dl.id = "fontList";
  dl.innerHTML = m.installed_fonts.map(f => `<option value="${esc(f)}">`).join("");
  document.body.appendChild(dl);
}).catch(() => {});

/* ---------------- options form ---------------- */
const form = $("#optForm");
const DEFAULT_OPTS = readForm();
function readForm() {
  const o = {};
  $$("input, select, textarea", form).forEach(el => {
    if (!el.name) return;
    if (el.type === "checkbox") o[el.name] = el.checked;
    else if (el.type === "number") o[el.name] = el.value === "" ? null : Number(el.value);
    else o[el.name] = el.value;
  });
  return o;
}
function writeForm(o) {
  $$("input, select, textarea", form).forEach(el => {
    if (!el.name || !(el.name in o)) return;
    if (el.type === "checkbox") el.checked = !!o[el.name]; else el.value = o[el.name] ?? "";
  });
  syncCustomSize();
}
function syncCustomSize() {
  const custom = form.page_size.value === "custom";
  $$(".custom-size").forEach(e => e.classList.toggle("hidden", !custom));
}
const OPTS_VERSION = 2;   // v2: remote images are downloaded by default
function savedOptions() {
  const o = Object.assign({}, DEFAULT_OPTS, store.get("options", {}));
  if (store.get("options_v", 1) < OPTS_VERSION) { o.download_remote = true; store.set("options", o); store.set("options_v", OPTS_VERSION); }
  return o;
}
writeForm(savedOptions());
form.addEventListener("input", () => { store.set("options", readForm()); syncCustomSize(); });
$("#resetOpts").onclick = () => { writeForm(DEFAULT_OPTS); store.set("options", DEFAULT_OPTS); toast("Options reset"); };

/* ---------------- source selection ---------------- */
let kind = "files";
let picked = [];   // [{file, rel}]
$$("#srcTabs button").forEach(b => b.onclick = () => {
  kind = b.dataset.kind;
  $$("#srcTabs button").forEach(x => x.classList.toggle("active", x === b));
  $$(".src-pane").forEach(p => p.classList.toggle("hidden", !p.dataset.pane.split(" ").includes(kind)));
  $("#dzText").innerHTML = {
    files: "<strong>Drop files or a folder here</strong><br><span class='muted'>or click to choose: HTML plus its CSS, images and fonts</span>",
    folder: "<strong>Drop a folder here</strong><br><span class='muted'>or click to choose the website folder</span>",
    zip: "<strong>Drop a .zip here</strong><br><span class='muted'>or click to choose a zipped website</span>",
  }[kind] || "";
});
const dz = $("#dropzone");
dz.onclick = () => ({ files: $("#fileInput"), folder: $("#folderInput"), zip: $("#zipInput") }[kind] || $("#fileInput")).click();
["fileInput", "folderInput", "zipInput"].forEach(id => $("#" + id).onchange = (e) => {
  addFiles(Array.from(e.target.files).map(f => ({ file: f, rel: f.webkitRelativePath || f.name })));
  e.target.value = "";
});
dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("over"); };
dz.ondragleave = () => dz.classList.remove("over");
dz.ondrop = async (e) => {
  e.preventDefault(); dz.classList.remove("over");
  const items = Array.from(e.dataTransfer.items || []);
  const out = [];
  const walk = (entry, prefix) => new Promise((res) => {
    if (entry.isFile) entry.file(f => { out.push({ file: f, rel: prefix + f.name }); res(); }, () => res());
    else if (entry.isDirectory) {
      const reader = entry.createReader(); const all = [];
      const readBatch = () => reader.readEntries(async (ents) => {
        if (!ents.length) { for (const en of all) await walk(en, prefix + entry.name + "/"); res(); }
        else { all.push(...ents); readBatch(); }
      }, () => res());
      readBatch();
    } else res();
  });
  const entries = items.map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (entries.length) { for (const en of entries) await walk(en, ""); }
  else Array.from(e.dataTransfer.files).forEach(f => out.push({ file: f, rel: f.name }));
  if (kind === "url" || kind === "paste") $('#srcTabs button[data-kind="files"]').click();
  addFiles(out);
};
function addFiles(list) {
  const seen = new Set(picked.map(p => p.rel));
  list.forEach(p => { if (!seen.has(p.rel)) { picked.push(p); seen.add(p.rel); } });
  renderPicked();
}
$("#clearFiles").onclick = () => { picked = []; renderPicked(); };
function renderPicked() {
  const box = $("#fileSummary");
  box.classList.toggle("hidden", !picked.length);
  if (!picked.length) return;
  const total = picked.reduce((a, p) => a + p.file.size, 0);
  $("#fileCountTxt").textContent = `${picked.length} file(s), ${fmtSize(total)}`;
  const kindOf = (n) => { const x = n.toLowerCase().split(".").pop();
    return { html: "page", htm: "page", css: "css", zip: "zip", ttf: "font", otf: "font", woff: "font", woff2: "font" }[x] ||
      (["png", "jpg", "jpeg", "gif", "svg", "webp", "tif", "tiff", "bmp"].includes(x) ? "image" : "file"); };
  const sorted = [...picked].sort((a, b) => (kindOf(a.rel) === "page" ? -1 : 0) - (kindOf(b.rel) === "page" ? -1 : 0));
  $("#fileList").innerHTML = sorted.slice(0, 300).map(p =>
    `<li><span>${esc(p.rel)}<span class="k">${kindOf(p.rel)}</span></span><span class="muted">${fmtSize(p.file.size)}</span></li>`).join("") +
    (picked.length > 300 ? `<li class="muted">… ${picked.length - 300} more</li>` : "");
  const htmls = picked.map(p => p.rel).filter(r => /\.(x?html?)$/i.test(r))
    .sort((a, b) => a.split("/").length - b.split("/").length || (/(^|\/)index\./i.test(a) ? -1 : 1));
  const hasZip = picked.some(p => /\.zip$/i.test(p.rel));
  $("#entryRow").classList.toggle("hidden", !htmls.length && !hasZip);
  $("#entrySelect").innerHTML = htmls.length ? htmls.map(h => `<option>${esc(h)}</option>`).join("")
    : `<option value="">Auto (index.html inside the zip)</option>`;
}

/* ---------------- convert ---------------- */
let rerunTarget = null;
$("#convertBtn").onclick = async () => {
  const opts = readForm();
  if (rerunTarget) {
    try {
      const job = await api(`/api/jobs/${rerunTarget}/rerun`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ options: opts }) });
      const id = rerunTarget; rerunTarget = null; setRerunMode(null);
      refreshJobs(); openJob(job.id || id);
    } catch (e) { toast(e.message, true); }
    return;
  }
  const fd = new FormData();
  fd.append("kind", kind); fd.append("options", JSON.stringify(opts)); fd.append("name", $("#jobName").value);
  if (kind === "url") fd.append("url", $("#urlInput").value.trim());
  else if (kind === "paste") fd.append("html", $("#htmlInput").value);
  else {
    if (!picked.length) return toast("Choose the files to convert first", true);
    picked.forEach(p => fd.append("files", p.file, p.rel));
    fd.append("entry", $("#entrySelect").value || "");
    fd.append("all_entries", $("#allEntries").checked ? "1" : "0");
  }
  const btn = $("#convertBtn"); btn.disabled = true; btn.textContent = "Uploading…";
  const up = $("#uploadProg"); up.classList.remove("hidden");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) { const p = e.loaded / e.total;
    $("i", up).style.width = (p * 100) + "%"; $("span", up).textContent = `Uploading ${Math.round(p * 100)}%`; } };
  xhr.onload = () => {
    btn.disabled = false; btn.textContent = "Convert to IDML"; up.classList.add("hidden");
    let data = {}; try { data = JSON.parse(xhr.responseText); } catch { /* */ }
    if (xhr.status >= 300) { toast(data.error || "Upload failed", true); if (data.id) { refreshJobs(); openJob(data.id); } return; }
    picked = []; renderPicked(); $("#jobName").value = "";
    refreshJobs(); openJob(data.id);
  };
  xhr.onerror = () => { btn.disabled = false; btn.textContent = "Convert to IDML"; up.classList.add("hidden"); toast("Network error", true); };
  xhr.send(fd);
};
function setRerunMode(job) {
  const h = $("#newView h1");
  if (job) { h.textContent = `Re-run: ${job.name}`; $("#convertBtn").textContent = "Re-run with these options";
    $$("#srcTabs, .src-pane, #jobName").forEach(e => e.closest(".card").classList.add("hidden")); }
  else { h.textContent = "New conversion"; $("#convertBtn").textContent = "Convert to IDML";
    $("#srcTabs").closest(".card").classList.remove("hidden"); }
}
$("#newBtn").onclick = () => { rerunTarget = null; setRerunMode(null); writeForm(savedOptions()); showView("new"); };

/* ---------------- views ---------------- */
function showView(v) {
  $("#newView").classList.toggle("hidden", v !== "new");
  $("#jobView").classList.toggle("hidden", v !== "job");
  if (v === "new") { currentJob = null; renderJobList(); }
}

/* ---------------- jobs list ---------------- */
let JOBS = [];
async function refreshJobs() {
  try { JOBS = await api("/api/jobs"); renderJobList(); } catch { /* server down */ }
}
function renderJobList() {
  const q = $("#jobSearch").value.toLowerCase();
  const list = JOBS.filter(j => !q || (j.name || "").toLowerCase().includes(q));
  $("#jobCount").textContent = JOBS.length ? JOBS.length : "";
  $("#jobList").innerHTML = list.map(j => `
    <li data-id="${esc(j.id)}" class="${j.id === currentJob ? "active" : ""}">
      <div class="jn" title="${esc(j.name)}">${esc(j.name)}</div>
      <div class="jm"><span class="chip ${esc(j.status)}">${esc(j.status)}</span>
        <span>${j.status === "done" && j.pages ? j.pages + " pages · " : ""}${ago(j.created)}</span></div>
      ${["queued", "running"].includes(j.status) ? `<div class="bar running"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div>` : ""}
    </li>`).join("") || `<li class="muted small">No conversions yet.</li>`;
  $$("#jobList li[data-id]").forEach(li => li.onclick = () => openJob(li.dataset.id));
}
$("#jobSearch").oninput = renderJobList;
setInterval(refreshJobs, 2500); refreshJobs();

/* ---------------- job view ---------------- */
let currentJob = null, JOB = null, pollTimer = null, resultIdx = 0, activeTab = "overview";
const IR = {};          // cache: `${jobId}:${idx}:${version}` -> summary
let edits = null;       // pending style edits for the styles tab

async function openJob(id) {
  if (currentJob !== id) { resultIdx = 0; edits = null; }
  currentJob = id; showView("job"); renderJobList();
  clearTimeout(pollTimer);
  await pollJob();
}
async function pollJob() {
  if (!currentJob) return;
  const id = currentJob;
  try {
    const prev = JOB;
    JOB = await api(`/api/jobs/${id}`);
    if (id !== currentJob) return;
    renderJob(prev);
  } catch (e) { toast(e.message, true); return; }
  if (["queued", "running", "new"].includes(JOB.status)) pollTimer = setTimeout(pollJob, 700);
}
function renderJob(prev) {
  const j = JOB;
  $("#jvName").textContent = j.name;
  const st = $("#jvStatus"); st.textContent = j.status; st.className = "chip " + j.status;
  $("#jvMeta").textContent = `${j.entries.length} document(s) · version ${j.version || 1} · ${new Date(j.created * 1000).toLocaleString()}`;
  const pct = Math.round((j.progress || 0) * 100);
  $("#jvStage").textContent = j.stage || "";
  $("#jvPct").textContent = pct + "%";
  const bar = $("#jvBar"); bar.style.width = pct + "%";
  bar.parentElement.className = "bar big " + (j.status === "done" ? "done" : j.status === "failed" ? "failed" : "running");
  $("#jvError").classList.toggle("hidden", !j.error); $("#jvError").textContent = j.error || "";
  const busy = ["queued", "running"].includes(j.status);
  $("#rerunBtn").disabled = busy; $("#deleteBtn").disabled = busy;
  renderResults();
  renderResultPicker();
  $("#logBox").innerHTML = j.log.map(l => `<span class="${l.level === "error" ? "e" : ""}">${new Date(l.t * 1000).toLocaleTimeString()}  ${esc(l.msg)}</span>`).join("\n");
  const finishedNow = prev && prev.id === j.id && prev.status !== j.status && j.status === "done";
  if (finishedNow) { toast("Conversion finished"); edits = null; }
  if (!prev || prev.id !== j.id || finishedNow || (prev.version !== j.version) || prev.status !== j.status) renderTab();
}
function dl(rel) { return `/api/jobs/${JOB.id}/download/${rel.split("/").map(encodeURIComponent).join("/")}`; }
function renderResults() {
  const j = JOB, area = $("#resultsArea");
  if (!j.results || !j.results.length) { area.innerHTML = ""; return; }
  const label = { package: "Package .zip", idml: ".idml", jsx: "Fit-pages script", report: "Report", ir: "IR JSON" };
  area.innerHTML = j.results.map((r, i) => `
    <div class="result">
      <div class="result-head">
        <div><strong>${esc(r.name)}.idml</strong> <span class="muted small">from ${esc(r.entry)} · ${r.seconds}s</span></div>
        <div class="downloads">
          ${r.outputs.package ? `<a class="btn primary" href="${dl(r.outputs.package)}">⬇ Download package (.zip)</a>` : ""}
          ${["idml", "jsx", "report", "ir"].filter(k => r.outputs[k]).map(k => `<a class="btn" href="${dl(r.outputs[k])}">${label[k]}</a>`).join("")}
        </div>
      </div>
      <div class="stats">
        ${stat(r.stats.pages, "pages (estimated)")}${stat(r.stats.paragraphs, "paragraphs")}${stat(r.stats.tables, "tables")}
        ${stat(r.stats.images, "images")}${stat(r.stats.hyperlinks, "hyperlinks")}${stat(r.paragraph_styles, "paragraph styles")}
        ${stat(r.character_styles, "character styles")}${stat(r.colors, "colour swatches")}${stat(Object.keys(r.fonts_used || {}).length, "font families")}
      </div>
    </div>`).join("") +
    (j.results.length > 1 ? `<div class="actions"><a class="btn primary" href="/api/jobs/${j.id}/download-zip">⬇ Download all documents</a></div>` : "");
}
const stat = (v, l) => `<div class="stat"><b>${v ?? 0}</b><span>${l}</span></div>`;
function renderResultPicker() {
  const box = $("#resultPicker");
  if (!JOB.results || JOB.results.length < 2 || ["log"].includes(activeTab)) { box.innerHTML = ""; return; }
  box.innerHTML = `<label>Document <select id="resSel">${JOB.results.map((r, i) =>
    `<option value="${i}" ${i === resultIdx ? "selected" : ""}>${esc(r.entry)}</option>`).join("")}</select></label>`;
  $("#resSel").onchange = (e) => { resultIdx = Number(e.target.value); edits = null; renderTab(); };
}
$$("#jobTabs button").forEach(b => b.onclick = () => {
  activeTab = b.dataset.tab;
  $$("#jobTabs button").forEach(x => x.classList.toggle("active", x === b));
  $$("#jobView .tab").forEach(t => t.classList.toggle("hidden", t.dataset.tab !== activeTab));
  renderResultPicker(); renderTab();
});
$("#deleteBtn").onclick = async () => {
  if (!confirm(`Delete "${JOB.name}" and all of its files?`)) return;
  try { await api(`/api/jobs/${JOB.id}`, { method: "DELETE" }); toast("Deleted"); currentJob = null; refreshJobs(); showView("new"); }
  catch (e) { toast(e.message, true); }
};
$("#rerunBtn").onclick = () => {
  rerunTarget = JOB.id;
  const o = Object.assign({}, DEFAULT_OPTS, JOB.options || {});
  if ((JOB.warnings || []).some(w => /remote (resource|file) skipped/.test(w))) {
    o.download_remote = true; toast("Remote image downloads switched on for this re-run");
  }
  writeForm(o);
  setRerunMode(JOB); showView("new"); currentJob = null; renderJobList();
};

async function loadIR() {
  const key = `${JOB.id}:${resultIdx}:${JOB.version}`;
  if (!IR[key]) IR[key] = await api(`/api/jobs/${JOB.id}/results/${resultIdx}/ir`);
  return IR[key];
}
function tabEl(name) { return $(`#jobView .tab[data-tab="${name}"]`); }
async function renderTab() {
  const el = tabEl(activeTab);
  if (activeTab === "log") return;
  const ready = JOB.status === "done" && JOB.results && JOB.results.length;
  if (activeTab === "compare") return renderCompare(el);
  if (activeTab === "overview") return renderOverview(el);
  if (!ready) { el.innerHTML = `<div class="empty">${JOB.status === "failed" ? "The conversion failed - see the log." : "Available when the conversion is done…"}</div>`; return; }
  el.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const ir = await loadIR();
    if (activeTab === "styles") renderStyles(el, ir);
    else if (activeTab === "outline") renderOutline(el, ir);
    else if (activeTab === "assets") renderAssets(el, ir);
  } catch (e) { el.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}
function renderOverview(el) {
  const j = JOB;
  const fonts = {};
  (j.results || []).forEach(r => Object.entries(r.fonts_used || {}).forEach(([f, s]) => { fonts[f] = new Set([...(fonts[f] || []), ...s]); }));
  const opts = j.options || {};
  const missing = j.missing_urls || [];
  el.innerHTML = `
    ${missing.length && j.status === "done" ? `<h3>Missing images (${missing.length})</h3>
      <p class="muted small">These could not be loaded. Open each link in your browser, save the image
      (right-click › Save image as…), choose the saved file here and re-convert - it is placed exactly where the original was.</p>
      <div class="scroll"><table class="tbl" id="missingTbl"><tr><th>Image</th><th>Reason</th><th>Your copy</th></tr>
      ${missing.map((m, i) => `<tr><td style="max-width:420px;word-break:break-all">
          ${/^https?:/.test(m.url) ? `<a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.url)}</a>` : esc(m.url)}</td>
        <td class="muted">${esc(m.reason)}</td>
        <td><input type="file" accept="image/*,.svg,.pdf,.eps,.psd,.tif,.tiff" data-url="${esc(m.url)}"></td></tr>`).join("")}
      </table></div>
      <div class="actions"><button class="btn primary" id="replaceBtn" disabled>Re-convert with these images</button></div>` : ""}
    ${j.warnings.length ? `<h3>Warnings (${j.warnings.length})</h3><ul class="warn-list">${j.warnings.slice(0, 200).map(w => `<li>${esc(w)}</li>`).join("")}</ul>`
      : (j.status === "done" ? `<p>✔ No warnings.</p>` : "")}
    ${Object.keys(fonts).length ? `<h3>Fonts used</h3><p>${Object.entries(fonts).map(([f, s]) =>
      `<span class="tag">${esc(f)}: ${esc([...s].join(", "))}</span>`).join(" ")}</p>
      <p class="muted small">${opts.package_fonts !== false ? "The font files are in the package's Document fonts folder; InDesign activates them when the document opens." : "Font files are not packaged; install them on the InDesign machine."}</p>` : ""}
    ${j.available_entries && j.available_entries.length > 1 ? `<h3>HTML files in the upload</h3>
      <p class="muted small">Pick the documents to convert, then re-run.</p>
      <div class="row gap">${j.available_entries.map(e => `<label class="check"><input type="checkbox" class="entryChk" value="${esc(e)}" ${j.entries.includes(e) ? "checked" : ""}> ${esc(e)}</label>`).join("")}</div>
      <div class="actions"><button class="btn" id="entriesRerun">Convert selected</button></div>` : ""}
    <h3>How to open</h3>
    <ol class="muted small">
      <li>Download the package zip and unzip it on the InDesign computer (keep the folder together).</li>
      <li>Open the <b>.idml</b> in InDesign (File › Open).</li>
      <li>Optional: run the <b>.fit-pages.jsx</b> script (Window › Utilities › Scripts) to make the page count exact.</li>
    </ol>`;
  const rbtn = $("#replaceBtn", el);
  if (rbtn) {
    const inputs = $$("#missingTbl input[type=file]", el);
    inputs.forEach(inp => inp.onchange = () => {
      const n = inputs.filter(i => i.files.length).length;
      rbtn.disabled = !n; rbtn.textContent = n ? `Re-convert with ${n} image(s)` : "Re-convert with these images";
    });
    rbtn.onclick = async () => {
      const fd = new FormData();
      inputs.filter(i => i.files.length).forEach(i => { fd.append("url", i.dataset.url); fd.append("file", i.files[0], i.files[0].name); });
      rbtn.disabled = true; rbtn.textContent = "Uploading…";
      try { await api(`/api/jobs/${j.id}/replace-images`, { method: "POST", body: fd }); toast("Re-converting with your images…"); openJob(j.id); }
      catch (e) { toast(e.message, true); rbtn.disabled = false; }
    };
  }
  const btn = $("#entriesRerun", el);
  if (btn) btn.onclick = async () => {
    const entries = $$(".entryChk", el).filter(c => c.checked).map(c => c.value);
    if (!entries.length) return toast("Select at least one file", true);
    try { await api(`/api/jobs/${j.id}/rerun`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entries }) });
      openJob(j.id); } catch (e) { toast(e.message, true); }
  };
}
function renderCompare(el) {
  const j = JOB;
  const entry = (j.results && j.results[resultIdx] && j.results[resultIdx].entry) || j.entries[0];
  if (!entry) { el.innerHTML = `<div class="empty">No source.</div>`; return; }
  const src = j.source.kind === "url" ? entry : `/api/jobs/${j.id}/source/${entry.split("/").map(encodeURIComponent).join("/")}`;
  if (el.dataset.src === src) return;
  el.dataset.src = src;
  el.innerHTML = `<div class="compare"><p class="muted small">The original page as the browser shows it (scripts disabled) - compare it with the InDesign result.
    <a href="${esc(src)}" target="_blank" rel="noopener">Open in a new tab ↗</a></p>
    <iframe sandbox="allow-same-origin" src="${esc(src)}"></iframe></div>`;
}
function renderOutline(el, ir) {
  el.innerHTML = `<p class="muted small">${ir.outline_total} blocks in reading order${ir.outline_total > ir.outline.length ? ` (first ${ir.outline.length} shown)` : ""}.
     The style shown is the InDesign paragraph style; tags list character styles and local overrides.</p>
    <div class="scroll">${ir.outline.map(o => `<div class="outline-item"><span class="st" title="${esc(o.style)}">${esc(o.style)}</span>
      <span>${esc(o.text)} ${(o.chars || []).map(c => `<span class="tag">${esc(c)}</span>`).join("")}
      ${(o.overrides || []).length ? `<span class="tag" title="${esc(o.overrides.join(", "))}">${o.overrides.length} override(s)</span>` : ""}</span></div>`).join("")}</div>`;
}
function renderAssets(el, ir) {
  const imgs = ir.assets.filter(a => a.kind === "image"), fonts = ir.assets.filter(a => a.kind === "font");
  const url = (a) => `/api/jobs/${JOB.id}/asset/${resultIdx}/${(a.rel || a.file).split("/").map(encodeURIComponent).join("/")}`;
  el.innerHTML = `<h3>Images (${imgs.length})</h3><div class="assets">${imgs.map(a => `
      <div class="asset"><div class="thumb">${a.rel ? `<img src="${url(a)}" alt="">` : ""}</div>
      <div class="meta"><b>${esc(a.file)}</b><br>${esc(a.format)} · ${a.size[0]}×${a.size[1]} px · ${a.ppi} ppi<br>
      <span class="muted" title="${esc(a.source)}">${esc(String(a.source).slice(0, 40))}</span></div></div>`).join("") || `<span class="muted">None</span>`}</div>
    <h3>Fonts (${fonts.length})</h3><div class="scroll"><table class="tbl"><tr><th>File</th><th>Family</th><th>Source</th></tr>
      ${fonts.map(a => `<tr><td>${esc(a.file)}</td><td>${esc(a.family)}</td><td class="muted">${esc(a.source)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">None</td></tr>`}</table></div>`;
}

/* ---------------- style editor ---------------- */
const PCOLS = [
  ["font_family", "Font", "font"], ["font_style", "Style", "text"], ["font_size", "Size", "num"],
  ["leading", "Leading", "lead"], ["color", "Colour", "color"], ["align", "Align", "align"],
  ["space_before", "Space ↑", "num"], ["space_after", "Space ↓", "num"], ["left_indent", "Indent ←", "num"],
  ["first_line_indent", "1st line", "num"], ["tracking", "Track", "num"],
];
const CCOLS = [
  ["font_family", "Font", "font"], ["font_style", "Style", "text"], ["font_size", "Size", "num"],
  ["color", "Colour", "color"], ["highlight", "Highlight", "color0"], ["underline", "U", "bool"],
  ["strike", "S", "bool"], ["caps", "Caps", "caps"], ["position", "Position", "pos"], ["tracking", "Track", "num"],
];
function cell(kind, key, type, val, hex) {
  const d = `data-k="${key}"`;
  switch (type) {
    case "font": return `<input ${d} list="fontList" value="${esc(val ?? "")}" placeholder="(inherit)">`;
    case "num": return `<input ${d} class="num" type="number" step="0.25" value="${val === undefined || val === null ? "" : round(val)}">`;
    case "lead": return `<input ${d} class="num" value="${val === undefined || val === null ? "" : esc(round(val))}" placeholder="auto">`;
    case "color": case "color0": return `<input ${d} type="color" value="${hex || (type === "color0" ? "#ffffff" : "#000000")}" ${type === "color0" && !val ? 'data-empty="1" title="none - pick to set"' : ""}>`;
    case "align": return `<select ${d}>${["left", "center", "right", "justify"].map(a => `<option ${a === val ? "selected" : ""}>${a}</option>`).join("")}</select>`;
    case "bool": return `<input ${d} type="checkbox" ${val ? "checked" : ""}>`;
    case "caps": return `<select ${d}>${["normal", "all_caps", "small_caps"].map(a => `<option ${a === (val || "normal") ? "selected" : ""}>${a}</option>`).join("")}</select>`;
    case "pos": return `<select ${d}>${["normal", "superscript", "subscript"].map(a => `<option ${a === (val || "normal") ? "selected" : ""}>${a}</option>`).join("")}</select>`;
    default: return `<input ${d} value="${esc(val ?? "")}">`;
  }
}
function renderStyles(el, ir) {
  edits = edits || { paragraph_styles: {}, character_styles: {}, page: {} };
  const colorHex = Object.fromEntries(ir.colors.map(c => [c.name, c.hex]));
  const pnames = ir.paragraph_styles.map(s => s.name);
  const prow = (s) => `<tr data-kind="paragraph_styles" data-name="${esc(s.name)}">
      <td><input data-k="__rename" value="${esc(s.name)}" title="source: ${esc(s.source)}"></td>
      <td><select data-k="__based_on"><option value="">[none]</option>${pnames.filter(n => n !== s.name).map(n => `<option ${n === s.based_on ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></td>
      <td class="muted">${s.uses}</td>
      ${PCOLS.map(([k, , t]) => `<td>${cell("p", k, t, s.effective[k], colorHex[s.effective[k]])}</td>`).join("")}
      <td><button class="btn small" data-json>{ }</button></td></tr>`;
  const crow = (s) => `<tr data-kind="character_styles" data-name="${esc(s.name)}">
      <td><input data-k="__rename" value="${esc(s.name)}" title="source: ${esc(s.source)}"></td>
      <td class="muted">${s.uses}</td>
      ${CCOLS.map(([k, , t]) => `<td>${cell("c", k, t, s.effective[k], colorHex[s.effective[k]])}</td>`).join("")}
      <td><button class="btn small" data-json>{ }</button></td></tr>`;
  const pg = ir.page;
  const mm = (v) => round(v * 25.4 / 72, 2);
  el.innerHTML = `
    <div class="editor-bar">
      <span class="muted small">Edit any value; changed rows are highlighted. Saving rewrites the IR and rebuilds the IDML - downloads update automatically.</span>
      <div class="row gap"><span id="editCount" class="muted small"></span>
        <button class="btn ghost" id="discardEdits">Discard</button>
        <button class="btn primary" id="saveEdits">Save &amp; rebuild IDML</button></div>
    </div>
    <h3>Page</h3>
    <div class="grid" id="pageEdit">
      ${[["width", "Width (mm)"], ["height", "Height (mm)"], ["margin_top", "Top margin (mm)"], ["margin_bottom", "Bottom (mm)"],
         ["margin_left", "Left/inside (mm)"], ["margin_right", "Right/outside (mm)"], ["gutter", "Gutter (mm)"]].map(([k, l]) =>
        `<label>${l}<input type="number" step="0.5" data-pk="${k}" value="${mm(pg[k])}"></label>`).join("")}
      <label>Columns<input type="number" min="1" data-pk="columns" value="${pg.columns}"></label>
      <label>Pages<input type="number" min="0" data-pk="page_count" value="${pg.page_count || ""}" placeholder="auto"></label>
      <label class="check"><input type="checkbox" data-pk="facing_pages" ${pg.facing_pages ? "checked" : ""}> Facing pages</label>
    </div>
    <h3>Paragraph styles (${ir.paragraph_styles.length})</h3>
    <div class="scroll"><table class="tbl"><tr><th>Name</th><th>Based on</th><th>Uses</th>${PCOLS.map(c => `<th>${c[1]}</th>`).join("")}<th></th></tr>
      ${ir.paragraph_styles.map(prow).join("")}</table></div>
    <h3>Character styles (${ir.character_styles.length})</h3>
    <div class="scroll"><table class="tbl"><tr><th>Name</th><th>Uses</th>${CCOLS.map(c => `<th>${c[1]}</th>`).join("")}<th></th></tr>
      ${ir.character_styles.map(crow).join("") || `<tr><td colspan="12" class="muted">None</td></tr>`}</table></div>
    <h3>Colours (${ir.colors.length})</h3>
    <p>${ir.colors.map(c => `<span class="tag"><span class="swatch" style="background:${esc(c.hex)}"></span>${esc(c.name)}</span>`).join(" ")}</p>`;

  const byName = { paragraph_styles: Object.fromEntries(ir.paragraph_styles.map(s => [s.name, s])),
                   character_styles: Object.fromEntries(ir.character_styles.map(s => [s.name, s])) };
  const updateCount = () => {
    const n = Object.keys(edits.paragraph_styles).length + Object.keys(edits.character_styles).length + (Object.keys(edits.page).length ? 1 : 0);
    $("#editCount").textContent = n ? `${n} pending change(s)` : "";
    $("#saveEdits").disabled = !n;
  };
  const toPt = (k, v) => ["columns", "page_count", "facing_pages"].includes(k) ? v : (v === "" ? "" : Number(v) * 72 / 25.4);
  $$("#pageEdit [data-pk]", el).forEach(inp => inp.oninput = inp.onchange = () => {
    const k = inp.dataset.pk; const v = inp.type === "checkbox" ? inp.checked : inp.value;
    edits.page[k] = toPt(k, v); updateCount();
  });
  $$("tr[data-kind]", el).forEach(tr => {
    const kindKey = tr.dataset.kind, name = tr.dataset.name, st = byName[kindKey][name];
    const ensure = () => (edits[kindKey][name] = edits[kindKey][name] || { props: {} });
    $$("[data-k]", tr).forEach(inp => {
      inp.oninput = inp.onchange = () => {
        const k = inp.dataset.k;
        const e = ensure();
        if (k === "__rename") { if (inp.value.trim() && inp.value.trim() !== name) e.rename = inp.value.trim(); else delete e.rename; }
        else if (k === "__based_on") e.based_on = inp.value;
        else {
          let v = inp.type === "checkbox" ? inp.checked : inp.value;
          if (inp.type === "color") inp.removeAttribute("data-empty");
          e.props[k] = v;
        }
        if (!Object.keys(e.props).length && !e.rename && !("based_on" in e)) delete edits[kindKey][name];
        tr.classList.toggle("changed", !!edits[kindKey][name]);
        updateCount();
      };
    });
    $("[data-json]", tr).onclick = () => {
      const current = JSON.stringify(Object.assign({}, st.props, (edits[kindKey][name] || {}).props), null, 1);
      const next = prompt(`Raw IR properties for "${name}" (JSON). Any IDML attribute can be added as "idml:AttributeName".`, current);
      if (next === null) return;
      try { const obj = JSON.parse(next); ensure().props = obj; tr.classList.add("changed"); updateCount(); }
      catch (err) { toast("Invalid JSON: " + err.message, true); }
    };
    if (edits[kindKey][name]) tr.classList.add("changed");
  });
  $("#discardEdits").onclick = () => { edits = null; renderStyles(el, ir); };
  $("#saveEdits").onclick = async () => {
    try {
      await api(`/api/jobs/${JOB.id}/results/${resultIdx}/edit`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(edits) });
      edits = null; toast("Rebuilding IDML…"); clearTimeout(pollTimer); pollJob();
    } catch (e) { toast(e.message, true); }
  };
  updateCount();
}
})();
