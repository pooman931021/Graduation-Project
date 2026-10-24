// Phase 7 前端邏輯（2026-07-26）。與 src/api/main.py 的 REST API 契約保持一致：
//   POST /api/upload                  上傳影片 + 觸發完整流程
//   GET  /api/jobs/{id}/status        查詢狀態＋各階段耗時
//   GET  /api/jobs/{id}/results       取得指定通道的匯出結果
//   GET  /api/jobs/{id}/frame         取得指定時間戳記最接近的真實 OCR 影格
//   POST /api/jobs/{id}/export        匯出 CSV
//   GET  /api/environment             一鍵確認環境是否齊全（Phase 7 測試用，見下方）
//
// 本檔案分兩部分：
//   1) 「測試用工具區」相關邏輯（環境檢查、參數/通道選擇、階段耗時顯示）——
//      這部分連同 index.html 裡的 <section id="test-panel"> 在成品完成後
//      會被使用者要求整個移除，移除方式：刪掉該 <section> + 這裡對應的
//      事件綁定與函式即可，不影響下面「核心功能」的任何程式碼。
//   2) 「核心功能」：檔案選取／開始／輪詢狀態／顯示結果／匯出——這部分是
//      正式成品也會保留的功能。

const API_BASE = ""; // 與後端同源（由 FastAPI 掛載靜態檔案提供），見 src/api/main.py

let currentJobId = null;
let pollTimer = null;
let lastAvailableChannels = [];
let lastLogId = 0;

const MAX_DISPLAY_ROWS = 200; // 結果筆數可能高達上萬筆，畫面只顯示前 N 筆，完整資料請用匯出功能

// ------------------------------------------------------------------
// 1) 測試用工具區
// ------------------------------------------------------------------

function toggleChannel1Engines() {
  const enabled = document.getElementById("channel1-toggle").checked;
  document.querySelectorAll(".c1-engine").forEach((cb) => { cb.disabled = !enabled; });
}

async function runEnvironmentCheck() {
  const btn = document.getElementById("env-check-btn");
  const list = document.getElementById("env-check-results");
  btn.disabled = true;
  list.innerHTML = "<li>檢查中...（KoNLPy 引擎初始化可能需要幾秒到十幾秒，請稍候）</li>";
  try {
    const resp = await fetch(`${API_BASE}/api/environment`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    list.innerHTML = "";
    for (const item of body.items) {
      const li = document.createElement("li");
      li.className = item.ok ? "ok" : "fail";
      li.textContent = `${item.ok ? "有" : "無"}｜${item.name}｜${item.detail}`;
      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li class="fail">檢查失敗：${e.message}</li>`;
  } finally {
    btn.disabled = false;
  }
}

function renderStageTiming(stages) {
  const tbody = document.querySelector("#stage-timing-table tbody");
  tbody.innerHTML = "";
  for (const s of stages) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${escapeHtml(s.stage)}</td>` +
      `<td>${s.ok ? "成功" : "失敗"}</td>` +
      `<td>${s.elapsed_sec}</td>` +
      `<td>${escapeHtml(s.error || "")}</td>`;
    tbody.appendChild(tr);
  }
}

// ------------------------------------------------------------------
// 2) 核心功能
// ------------------------------------------------------------------

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

async function startJob() {
  const fileInput = document.getElementById("video-file");
  if (!fileInput.files.length) {
    alert("請先選取影片檔案");
    return;
  }

  const options=window.LocalMedia.options();
  if (options.recognition_mode.startsWith('gemini_') && !window.AISettings.enabled()) {
    document.getElementById('job-status-line').textContent='請先設定自己的 Gemini API，或選擇不需 API 的本機模式。';
    document.getElementById('ai-settings-btn').click();return;
  }
  const form = new FormData();
  form.append("file", fileInput.files[0]);
  Object.entries(options).forEach(([key,value])=>form.append(key,value));

  // --- 測試用參數（見 1) 段落說明，成品完成後這些欄位會被移除或改為固定值）---
  form.append("denoise", document.getElementById("param-denoise").checked);
  form.append("sample_rate_fps", document.getElementById("param-fps").value || "1.0");

  const maxFrames = document.getElementById("param-max-frames").value;
  if (maxFrames) form.append("max_frames", maxFrames);
  const model = document.getElementById("param-model").value;
  if (model) form.append("model", model);
  const baseUrl = document.getElementById("param-base-url").value;
  if (baseUrl) form.append("base_url", baseUrl);
  const minConfidence = document.getElementById("param-min-confidence").value;
  if (minConfidence) form.append("min_confidence", minConfidence);
  const dedupGap = document.getElementById("param-dedup-gap").value;
  if (dedupGap) form.append("dedup_max_gap_sec", dedupGap);
  const clahe = document.getElementById("param-clahe").value;
  if (clahe) form.append("clahe_clip_limit", clahe);
  const denoiseStrength = document.getElementById("param-denoise-strength").value;
  if (denoiseStrength) form.append("denoise_strength", denoiseStrength);

  const channel1On = document.getElementById("channel1-toggle").checked;
  form.append("include_channel1", channel1On);
  if (channel1On) {
    const engines = Array.from(document.querySelectorAll(".c1-engine:checked")).map((cb) => cb.value);
    form.append("channel1_engines", engines.join(","));
  }
  form.append("include_channel2", document.getElementById("channel2-toggle").checked);
  form.append("include_channel3", document.getElementById("channel3-toggle").checked);
  // --- 測試用參數結束 ---

  const startBtn = document.getElementById("start-btn");
  const cancelBtn = document.getElementById("cancel-btn");
  startBtn.disabled = true;
  window.Learning.beginJob();
  window.Learning.setBusy(true);
  document.getElementById("video-file").disabled = true;
  currentJobId = null;
  if (pollTimer) clearInterval(pollTimer);
  try { sessionStorage.removeItem("currentJobId"); } catch (_) {}
  cancelBtn.style.display = "inline-block";
  cancelBtn.disabled = false;
  document.getElementById("job-status-line").textContent = "上傳中...";
  document.getElementById("results-controls").hidden = true;
  document.getElementById("results-table").hidden = true;
  lastAvailableChannels = [];
  lastLogId = 0;
  const logBox = document.getElementById("realtime-log-box");
  if (logBox) logBox.textContent = "[系統] 正在讀取並上傳影片檔案至伺服器...\n";

  try {
    const resp = await fetch(`${API_BASE}/api/upload`, { method: "POST", body: form });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);

    currentJobId = body.job_id;
    window.Learning.attachJob(currentJobId);
    try { sessionStorage.setItem("currentJobId", currentJobId); } catch (_) {}
    try { sessionStorage.setItem("learningFileIdentity", window.Learning.fileIdentity()); } catch (_) {}
    document.getElementById("job-status-line").textContent =
      `工作已建立：${currentJobId}（狀態：${body.status}）`;
    appendLogMessage(`[系統] 伺服器已建立任務 ${currentJobId}，啟動背景管線處理...`);
    startPolling();
  } catch (e) {
    document.getElementById("job-status-line").textContent = `上傳失敗：${e.message}`;
    appendLogMessage(`[錯誤] 影片上傳失敗：${e.message}`);
    startBtn.disabled = false;
    window.Learning.setBusy(false);
    document.getElementById("video-file").disabled = false;
    cancelBtn.style.display = "none";
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollStatus, 1000);
  pollStatus();
}

function appendLogMessage(msg) {
  const box = document.getElementById("realtime-log-box");
  if (!box) return;
  if (box.textContent.trim() === "尚未開始處理影片..." || box.textContent.includes("尚未開始處理影片...")) {
    box.textContent = "";
  }
  box.textContent += (box.textContent ? "\n" : "") + msg;
  if (document.getElementById("autoscroll-toggle")?.checked) {
    box.scrollTop = box.scrollHeight;
  }
}

async function pollLogs() {
  if (!currentJobId) return;
  try {
    const resp = await fetch(`${API_BASE}/api/jobs/${currentJobId}/logs?after_id=${lastLogId}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.logs && data.logs.length > 0) {
      for (const item of data.logs) {
        appendLogMessage(item.message);
        if (item.id > lastLogId) {
          lastLogId = item.id;
        }
      }
    }
  } catch (e) {
    // 忽略暫態輪詢錯誤
  }
}

async function cancelJob() {
  if (!currentJobId) return;
  if (!confirm("確定要中斷目前的影片處理運算嗎？")) return;
  const cancelBtn = document.getElementById("cancel-btn");
  cancelBtn.disabled = true;
  appendLogMessage("[系統] 正在發送中斷請求...");
  try {
    const resp = await fetch(`${API_BASE}/api/jobs/${currentJobId}/cancel`, { method: "POST" });
    const data = await resp.json();
    appendLogMessage(`[系統] ${data.message || "已送出中斷請求"}`);
  } catch (e) {
    appendLogMessage(`[錯誤] 發送中斷請求失敗：${e.message}`);
    cancelBtn.disabled = false;
  }
}

async function shutdownSystem() {
  const proceed = confirm(
    "【關閉系統確認】\n\n確定要結束後端伺服器並關閉系統嗎？\n" +
    "（這將會終止 CMD 視窗與背景服務，釋放所有運算資源與通訊埠）"
  );
  if (!proceed) return;

  appendLogMessage("[系統] 正在發送關閉伺服器指令...");
  try {
    const resp = await fetch(`${API_BASE}/api/system/shutdown`, { method: "POST" });
    const data = await resp.json();
    document.getElementById("job-status-line").innerHTML =
      `<strong style="color: #b3261e;">🛑 系統已成功關閉。後端程序已終止，您可以安心關閉此瀏覽器分頁。</strong>`;
    appendLogMessage(`[系統] ${data.message || "服務已終止"}`);
    document.getElementById("start-btn").disabled = true;
    document.getElementById("system-shutdown-btn").disabled = true;
    document.getElementById("cancel-btn").style.display = "none";
  } catch (e) {
    appendLogMessage(`[系統] 連線已中斷（伺服器已成功關閉）：${e.message}`);
    document.getElementById("job-status-line").innerHTML =
      `<strong style="color: #b3261e;">🛑 系統已關閉或連線中斷。</strong>`;
  }
}

async function pollStatus() {
  if (!currentJobId) return;
  await pollLogs();
  let resp;
  try { resp = await fetch(`${API_BASE}/api/jobs/${currentJobId}/status`); }
  catch (_) { document.getElementById("job-status-line").textContent = "暫時無法連線後端，正在重試…"; return; }
  if (resp.status === 404) {
    clearInterval(pollTimer); pollTimer = null;
    document.getElementById("start-btn").disabled = false;
    document.getElementById("video-file").disabled = false;
    document.getElementById("job-status-line").textContent = "找不到上次工作，請重新選擇影片開始。";
    try { sessionStorage.removeItem("currentJobId"); } catch (_) {}
    return;
  }
  if (!resp.ok) return;
  const body = await resp.json();
  window.Learning.attachJob(body.job_id);
  if (body.recognition) window.LocalMedia.restoreResult(body.job_id,body.recognition);
  const running=body.status==='running' || body.status==='queued';
  document.getElementById('start-btn').disabled=running;
  document.getElementById('video-file').disabled=running;
  window.Learning.setBusy(running);
  document.getElementById('cancel-btn').style.display=running?'inline-block':'none';

  if (body.source) window.Learning.setSource(body.source);
  else if (body.video_url) window.Learning.restoreLocalVideo(body.job_id,body.original_filename,body.video_url,body.playback_ready);

  document.getElementById("job-status-line").textContent = body.source
    ? `已載入 ${body.source.subtitle_count} 段韓文${body.source.is_generated ? '自動' : '人工'}字幕，可播放影片或點選字幕學習。${body.source.warning || ''}`
    : `狀態：${body.status}｜目前階段：${body.current_stage || "-"}` + (body.error ? `｜錯誤：${body.error}` : "");

  if (!body.source) {
    const line=document.getElementById('job-status-line');
    const states={queued:'排隊中',running:'辨識中',done:'完成',failed:'辨識失敗',cancelled:'已停止'};
    line.textContent=body.recognition && body.status==='done'
      ? `${body.recognition.label} · ${body.recognition.subtitle_count} 段字幕${body.recognition.limit?'（僅辨識所選的前段範圍）':''}。${body.recognition.warning||''}`
      : `${states[body.status]||body.status} · ${body.current_stage||'準備中'}${body.error?'｜'+body.error:''}`;
  }
  renderStageTiming(body.stages || []);
  updateChannelSelect(body.available_channels || []);

  if (body.status === "done" || body.status === "failed" || body.status === "cancelled") {
    clearInterval(pollTimer);
    pollTimer = null;
    await pollLogs();
    document.getElementById("start-btn").disabled = false;
    window.Learning.setBusy(false);
    document.getElementById("video-file").disabled = false;
    document.getElementById("cancel-btn").style.display = "none";
  }
}

function updateChannelSelect(channels) {
  const changed = JSON.stringify(channels) !== JSON.stringify(lastAvailableChannels);
  if (!changed) return;
  lastAvailableChannels = channels;

  const select = document.getElementById("channel-select");
  const previousValue = select.value;
  select.innerHTML = "";
  for (const ch of channels) {
    const opt = document.createElement("option");
    opt.value = ch;
    opt.textContent = ({channel1_mecab:'Mecab 韓文斷詞＋字典',channel1_okt:'Okt 韓文斷詞＋字典',channel1_komoran:'Komoran 韓文斷詞＋字典',channel1_kkma:'Kkma 韓文斷詞＋字典',channel2:'語意模型',channel3:'字典＋語意模型'})[ch] || ch;
    select.appendChild(opt);
  }
  if (channels.includes(previousValue)) select.value = previousValue;

  const controls = document.getElementById("results-controls");
  if (channels.length > 0) {
    controls.hidden = false;
    loadResultsForSelectedChannel();
  } else {
    controls.hidden = true;
  }
}

async function loadResultsForSelectedChannel() {
  const channel = document.getElementById("channel-select").value;
  if (!currentJobId || !channel) return;

  const requestedJob = currentJobId;
  let resp;
  try { resp = await fetch(
    `${API_BASE}/api/jobs/${currentJobId}/results?channel=${encodeURIComponent(channel)}`,
  ); } catch (_) { return; }
  if (!resp.ok) return;
  const body = await resp.json();
  if (requestedJob !== currentJobId || channel !== document.getElementById("channel-select").value) return;
  renderResultsTable(body);
  await window.Learning.showResults(body, currentJobId);
}

function renderResultsTable(body) {
  const table = document.getElementById("results-table");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  table.hidden = false;

  const rows = body.records.slice(0, MAX_DISPLAY_ROWS);
  for (const r of rows) {
    const tr = document.createElement("tr");

    const imgTd = document.createElement("td");
    if (String(r.備註 || '').includes('YouTube')) {
      imgTd.textContent = 'YouTube 字幕';
    } else if (r.timestamp_start !== null && r.timestamp_start !== undefined) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.className = "frame-thumb";
      img.alt = "影格截圖";
      img.src = `${API_BASE}/api/jobs/${currentJobId}/frame?timestamp=${r.timestamp_start}`;
      img.onerror = () => { imgTd.textContent = "(無截圖)"; };
      imgTd.appendChild(img);
    } else {
      imgTd.textContent = "(無時間戳記)";
    }
    tr.appendChild(imgTd);

    for (const field of ["原文", "詞性", "中文", "例句"]) {
      const td = document.createElement("td");
      td.textContent = r[field] ?? "";
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }

  const note = document.getElementById("results-note");
  note.textContent = body.records.length > MAX_DISPLAY_ROWS
    ? `顯示前 ${MAX_DISPLAY_ROWS} 筆（共 ${body.records.length} 筆），完整資料請使用「匯出檔案」`
    : `共 ${body.records.length} 筆`;
}

async function exportResults() {
  const channel = document.getElementById("channel-select").value;
  if (!currentJobId || !channel) return;

  const resp = await fetch(
    `${API_BASE}/api/jobs/${currentJobId}/export?format=csv&channel=${encodeURIComponent(channel)}`,
    { method: "POST" },
  );
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(`匯出失敗：${err.detail || resp.statusText}`);
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${currentJobId}_${channel}_export.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ------------------------------------------------------------------
// 事件綁定
// ------------------------------------------------------------------

document.getElementById("env-check-btn").addEventListener("click", runEnvironmentCheck);
document.getElementById("channel1-toggle").addEventListener("change", toggleChannel1Engines);
document.getElementById("start-btn").addEventListener("click", startJob);
document.getElementById("cancel-btn").addEventListener("click", cancelJob);
document.getElementById("system-shutdown-btn").addEventListener("click", shutdownSystem);
document.getElementById("clear-logs-btn").addEventListener("click", () => {
  const box = document.getElementById("realtime-log-box");
  if (box) box.textContent = "";
});
document.getElementById("channel-select").addEventListener("change", loadResultsForSelectedChannel);
document.getElementById("export-btn").addEventListener("click", exportResults);
document.getElementById("youtube-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (document.getElementById('youtube-load-btn').disabled) return;
  const url = document.getElementById('youtube-url').value.trim();
  if (!url) return;
  const previousJob = currentJobId;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  window.Learning.setBusy(true);
  document.getElementById('start-btn').disabled = true;
  document.getElementById('job-status-line').textContent = '正在取得韓文字幕並查詢字典，請稍候…';
  try {
    const response = await fetch('/api/youtube/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `載入失敗（${response.status}）`);
    window.Learning.beginJob();
    currentJobId = result.job_id; lastAvailableChannels = []; lastLogId = 0;
    document.getElementById('results-controls').hidden = true;
    document.getElementById('results-table').hidden = true;
    window.Learning.setSource(result.source);
    try {
      sessionStorage.setItem('currentJobId', currentJobId);
      sessionStorage.setItem('learningFileIdentity', window.Learning.fileIdentity());
    } catch (_) {}
    startPolling();
  } catch (error) {
    currentJobId = previousJob;
    document.getElementById('job-status-line').textContent = error.message;
  } finally {
    window.Learning.setBusy(false); document.getElementById('start-btn').disabled = false;
  }
});

toggleChannel1Engines();

// 自動復原進行中或最近一次工作
try {
  const savedJobId = sessionStorage.getItem("currentJobId");
  if (savedJobId) {
    currentJobId = savedJobId;
    window.Learning.restoreFile(sessionStorage.getItem("learningFileIdentity"));
    const logBox = document.getElementById("realtime-log-box");
    if (logBox && (logBox.textContent.trim() === "尚未開始處理影片..." || !logBox.textContent.trim())) {
      logBox.textContent = `[系統] 載入既有工作 ${currentJobId}，正在同步最新狀態與日誌...\n`;
    }
    startPolling();
  }
} catch (_) {}
