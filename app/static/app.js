"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  lastBox: null,
  packages: [],
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
  }
  if (!response.ok) {
    const err = new Error((data && (data.message || data.error)) || `HTTP ${response.status}`);
    err.status = response.status;
    err.data = data;
    throw err;
  }
  return data;
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function fmtRect(rect) {
  return `行 [${rect.row_start}, ${rect.row_end}) · 列 [${rect.col_start}, ${rect.col_end})`;
}

function showResult(el, cls, html) {
  el.hidden = false;
  el.className = `result ${cls}`;
  el.innerHTML = html;
}

/* ---------------- packages ---------------- */

async function loadPackages() {
  const data = await api("/api/packages");
  state.packages = data.packages;
  const tbody = $("#package-rows");
  $("#package-stats").textContent =
    `共 ${data.packages.length} 个包 · 最高版号 ${data.max_version} · 现行积水格 ${data.total_current_wet}`;

  if (!data.packages.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">尚无巡查包</td></tr>';
  } else {
    tbody.innerHTML = data.packages.map((p) => `
      <tr>
        <td class="num">${p.version}</td>
        <td>${fmtRect(p.coverage)}</td>
        <td class="num">${p.listed_wet_points}</td>
        <td class="num">${p.current_wet_points}</td>
        <td class="num">${p.archived_wet_points}${p.historical ? ` <span class="tag tag-old" title="升级前已在仓，仅保留升级时能确定的干湿">旧包</span>` : ` <span class="tag tag-new" title="升级后入仓，原报积水点全部归档">新包</span>`}</td>
        <td><code>${esc(p.digest.slice(0, 12))}…</code></td>
        <td>${esc(p.imported_at)}</td>
      </tr>
    `).join("");
  }
  populateHistoryVersions(data.packages);
}

function populateHistoryVersions(packages) {
  const select = $("#history-version");
  const previous = select.value;
  if (!packages.length) {
    select.innerHTML = '<option value="">（无已入仓包）</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = packages.map((p) =>
    `<option value="${p.version}">版号 ${p.version} · ${p.historical ? "旧包（升级时快照）" : "新包（原报全归档）"} · 原报 ${p.listed_wet_points} 点</option>`
  ).join("");
  if (previous && packages.some((p) => String(p.version) === previous)) {
    select.value = previous;
  }
}

async function importPackage() {
  const btn = $("#import-btn");
  const out = $("#import-result");
  const body = $("#package-json").value.trim();
  if (!body) {
    showResult(out, "bad", "请先粘贴巡查包 JSON 或读取文件。");
    return;
  }

  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch (err) {
    showResult(out, "bad", `<strong>未提交：JSON 语法错误</strong><ul><li>${esc(err.message)}</li></ul>`);
    return;
  }

  btn.disabled = true;
  try {
    const result = await api("/api/packages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
    });
    if (result.status === "unchanged") {
      showResult(out, "warn",
        `<span class="status-badge status-unchanged">未改变</span>
         <strong> 版号 ${result.version} 同内容重传</strong>
         <p>${esc(result.message)}</p>
         <p class="hint">摘要 <code>${esc(result.digest.slice(0, 16))}…</code> · 最高版号 ${result.max_version}</p>`);
    } else {
      const s = result.summary;
      const bits = [
        `覆盖 ${fmtRect(s.coverage)}（面积 ${s.coverage_area} 格，按矩形存储）`,
        `包内列出积水点 ${s.listed_wet_points}`,
        `新增现行积水 ${s.current_wet_added}`,
        `被本包覆盖清除 ${s.current_wet_removed}`,
      ];
      if (s.wet_points_shadowed_by_newer) {
        bits.push(`因更高版号已覆盖而保留新版结果 ${s.wet_points_shadowed_by_newer}`);
      }
      if (result.late_arrival) {
        bits.push("旧包晚到：只补新版未覆盖的区域");
      }
      const warn = result.warning
        ? `<p class="hint">注意：${esc(result.warning)}</p>`
        : "";
      showResult(out, "ok",
        `<span class="status-badge status-dry">已入仓</span>
         <strong> 版号 ${result.version}</strong>
         <ul>${bits.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>${warn}`);
    }
    await refreshAll();
  } catch (err) {
    const details = (err.data && err.data.details) || [];
    const code = err.status === 409 ? "退回：版号冲突" : "退回：包校验失败";
    showResult(out, "bad",
      `<strong>${esc(code)}</strong>
       <p>${esc(err.message)}</p>
       ${details.length ? `<ul>${details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
       <p class="hint">整包未提交，库中覆盖范围与现行积水保持原样。</p>`);
  } finally {
    btn.disabled = false;
    await loadPackages();
  }
}

/* ---------------- box query ---------------- */

function svgEl(name, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function drawMap(result) {
  const svg = $("#map");
  svg.innerHTML = "";
  const box = result.box;
  const w = box.col_end - box.col_start;
  const h = box.row_end - box.row_start;
  const scale = Math.max(w, h);
  const cellSize = 100 / scale;
  const offX = (100 - w * cellSize) / 2;
  const offY = (100 - h * cellSize) / 2;

  // Surveyed background: each coverage rectangle is a single SVG rect.
  for (const rect of result.coverage_rects) {
    const r0 = Math.max(rect.row_start, box.row_start);
    const r1 = Math.min(rect.row_end, box.row_end);
    const c0 = Math.max(rect.col_start, box.col_start);
    const c1 = Math.min(rect.col_end, box.col_end);
    if (r1 <= r0 || c1 <= c0) continue;
    svg.appendChild(svgEl("rect", {
      x: offX + (c0 - box.col_start) * cellSize,
      y: offY + (r0 - box.row_start) * cellSize,
      width: (c1 - c0) * cellSize,
      height: (r1 - r0) * cellSize,
      fill: "var(--dry-fill)",
      stroke: "var(--dry-line)",
      "stroke-width": 0.15,
    }));
  }

  // Current wet cells (already limited by the API).
  const maxCells = 4000;
  result.cells.slice(0, maxCells).forEach((cell) => {
    svg.appendChild(svgEl("rect", {
      x: offX + (cell.col - box.col_start) * cellSize,
      y: offY + (cell.row - box.row_start) * cellSize,
      width: Math.max(cellSize, 0.35),
      height: Math.max(cellSize, 0.35),
      fill: "var(--wet)",
    }));
  });

  // Query frame.
  svg.appendChild(svgEl("rect", {
    x: offX, y: offY, width: w * cellSize, height: h * cellSize,
    fill: "none", stroke: "var(--accent)", "stroke-width": 0.4,
    "stroke-dasharray": "1.6 1.2",
  }));
}

async function queryBox(event) {
  if (event) event.preventDefault();
  const form = $("#box-form");
  const fd = new FormData(form);
  const params = new URLSearchParams();
  for (const key of ["row_start", "row_end", "col_start", "col_end", "limit"]) {
    params.set(key, fd.get(key));
  }
  const summary = $("#box-summary");
  const rows = $("#wet-rows");
  try {
    const result = await api(`/api/wet?${params.toString()}`);
    state.lastBox = result;
    drawMap(result);
    const area = (result.box.row_end - result.box.row_start) * (result.box.col_end - result.box.col_start);
    summary.textContent =
      `框查 ${fmtRect(result.box)}（${area} 格）· 现行积水格 ${result.count}` +
      (result.truncated ? `（已达到返回上限 ${result.limit}，结果被截断）` : "") +
      ` · 覆盖矩形 ${result.coverage_rects.length} 个`;
    if (!result.cells.length) {
      rows.innerHTML = '<tr><td colspan="3" class="empty">框内没有现行积水格</td></tr>';
    } else {
      rows.innerHTML = result.cells.map((c) =>
        `<tr><td class="num">${c.row}</td><td class="num">${c.col}</td><td class="num">${c.version}</td></tr>`
      ).join("");
    }
  } catch (err) {
    summary.textContent = `框查失败：${err.message}`;
    rows.innerHTML = `<tr><td colspan="3" class="empty">${esc(err.message)}</td></tr>`;
  }
}

/* ---------------- single cell ---------------- */

async function queryCell(event) {
  if (event) event.preventDefault();
  const fd = new FormData($("#cell-form"));
  const out = $("#cell-result");
  const params = new URLSearchParams({ row: fd.get("row"), col: fd.get("col") });
  try {
    const result = await api(`/api/cell?${params.toString()}`);
    const cls = result.status === "wet" ? "status-wet"
      : result.status === "dry" ? "status-dry" : "status-unsurveyed";
    const version = result.version == null ? "" : ` · 生效版号 ${result.version}`;
    showResult(out, result.status === "wet" ? "bad" : "ok",
      `<strong>(${result.row}, ${result.col})</strong>
       <span class="status-badge ${cls}">${esc(result.label)}</span>
       <span class="hint">${esc(version)}</span>`);
  } catch (err) {
    showResult(out, "bad", `<strong>查询失败</strong><p>${esc(err.message)}</p>`);
  }
}

/* ---------------- historical box statistics ---------------- */

function drawHistoryMap(result) {
  const svg = $("#history-map");
  svg.innerHTML = "";
  if (!result.intersection) return;
  const ib = result.intersection;
  const w = ib.col_end - ib.col_start;
  const h = ib.row_end - ib.row_start;
  const scale = Math.max(w, h);
  const cellSize = 100 / scale;
  const offX = (100 - w * cellSize) / 2;
  const toX = (col) => offX + (col - ib.col_start) * cellSize;
  const toY = (row) => offY0() + (row - ib.row_start) * cellSize;
  function offY0() { return (100 - h * cellSize) / 2; }

  // Whole intersection: knowable region background.
  svg.appendChild(svgEl("rect", {
    x: offX, y: offY0(), width: w * cellSize, height: h * cellSize,
    fill: "var(--dry-fill)", stroke: "var(--dry-line)", "stroke-width": 0.15,
  }));
  // Unknown rectangles (frozen higher-version coverage).
  for (const rect of result.unknown_rects) {
    const r0 = Math.max(rect.row_start, ib.row_start);
    const r1 = Math.min(rect.row_end, ib.row_end);
    const c0 = Math.max(rect.col_start, ib.col_start);
    const c1 = Math.min(rect.col_end, ib.col_end);
    if (r1 <= r0 || c1 <= c0) continue;
    svg.appendChild(svgEl("rect", {
      x: toX(c0), y: toY(r0),
      width: (c1 - c0) * cellSize, height: (r1 - r0) * cellSize,
      fill: "var(--unknown-strong)", stroke: "var(--warn)", "stroke-width": 0.15,
      "stroke-dasharray": "1 0.8",
    }));
  }
  // Archived / snapshot wet cells.
  result.archived_cells.slice(0, 4000).forEach((cell) => {
    svg.appendChild(svgEl("rect", {
      x: toX(cell.col), y: toY(cell.row),
      width: Math.max(cellSize, 0.35), height: Math.max(cellSize, 0.35),
      fill: "var(--wet)",
    }));
  });
  // Intersection frame.
  svg.appendChild(svgEl("rect", {
    x: offX, y: offY0(), width: w * cellSize, height: h * cellSize,
    fill: "none", stroke: "var(--accent)", "stroke-width": 0.4,
    "stroke-dasharray": "1.6 1.2",
  }));
}

async function queryHistory(event) {
  if (event) event.preventDefault();
  const form = $("#history-form");
  const fd = new FormData(form);
  const out = $("#history-result");
  const summary = $("#history-summary");
  const params = new URLSearchParams();
  for (const key of ["version", "row_start", "row_end", "col_start", "col_end"]) {
    params.set(key, fd.get(key));
  }
  try {
    const result = await api(`/api/history/wet?${params.toString()}`);
    drawHistoryMap(result);
    if (!result.intersection) {
      summary.textContent = "工区矩形与该包覆盖范围没有交集，统计为 0 格。";
      showResult(out, "warn",
        `<strong>版号 ${result.version}：无交集</strong>
         <p>工区 ${fmtRect(result.box)} 不落在该包覆盖范围内，积水格确数为 <strong>0</strong>。</p>`);
      return;
    }
    const area = (result.intersection.row_end - result.intersection.row_start)
      * (result.intersection.col_end - result.intersection.col_start);
    const kind = result.historical ? "旧包（升级时快照）" : "新包（原报点全归档）";
    summary.textContent =
      `交集 ${fmtRect(result.intersection)}（${area} 格）· 已知湿 ${result.known_wet} · 已知干 ${result.known_dry} · 未知 ${result.unknown_cells} · 原报总数 ${result.listed_wet_total}`
      + (result.archived_truncated ? " · 归档点显示已截断" : "");

    if (result.exact) {
      showResult(out, "ok",
        `<span class="status-badge status-dry">确数</span>
         <strong> 版号 ${result.version}（${kind}）在工区内的积水格为 ${result.count} 格</strong>
         <ul>
           <li>交集面积 ${area} 格；已知积水 ${result.known_wet}、已知干 ${result.known_dry}、未知 ${result.unknown_cells}</li>
           <li>最少 = 最多 = ${result.min_wet}，两值相同，报确数。</li>
         </ul>`);
    } else {
      const reason = result.reasoning || {};
      showResult(out, "warn",
        `<span class="status-badge status-unchanged">区间估计</span>
         <strong> 版号 ${result.version}（${kind}）在工区内的积水格为
           <span class="num">${result.min_wet} – ${result.max_wet}</span> 格</strong>
         <ul>
           <li>交集面积 ${area} 格；已知积水 ${result.known_wet}、已知干 ${result.known_dry}、未知 ${result.unknown_cells}</li>
           <li>该包原报积水总数 ${result.listed_wet_total}；交集外已知湿 ${reason.wet_outside_intersection ?? "-"}、交集外未知 ${reason.unknown_outside_intersection ?? "-"}</li>
           <li>未知格中的原报点最多让上界到 ${result.max_wet}，最少可全部落在交集外，下界为 ${result.min_wet}。</li>
         </ul>
         <p class="hint">后来复测不会改变本结果：未知范围与已知干湿均为升级时冻结的同一份已提交图层。</p>`);
    }
  } catch (err) {
    summary.textContent = `复核失败：${err.message}`;
    showResult(out, "bad", `<strong>复核失败</strong><p>${esc(err.message)}</p>`);
  }
}

/* ---------------- samples / wiring ---------------- */

async function loadSample(name) {
  try {
    const text = await (await fetch(`/static/samples/${name}`)).text();
    $("#package-json").value = text;
    $("#import-result").hidden = true;
  } catch {
    $("#package-json").value = `无法加载示例 ${name}`;
  }
}

async function resetDemo() {
  if (!confirm("清空演示库（所有巡查包与积水状态）？")) return;
  const out = $("#import-result");
  try {
    await api("/api/reset", { method: "POST" });
  } catch (err) {
    showResult(out, "bad", `<strong>未清空</strong><p>${esc(err.message)}</p>
      <p class="hint">需以 FLOOD_ALLOW_RESET=1 启动服务后才启用清空。</p>`);
    return;
  }
  out.hidden = true;
  $("#box-summary").textContent = "已清空，可重新导入示例。";
  $("#wet-rows").innerHTML = '<tr><td colspan="3" class="empty">尚未框查</td></tr>';
  $("#cell-result").hidden = true;
  $("#history-summary").textContent = "";
  $("#history-result").hidden = true;
  $("#history-map").innerHTML = "";
  $("#map").innerHTML = "";
  await refreshAll();
}

async function refreshAll() {
  await loadPackages();
}

function wire() {
  $("#import-btn").addEventListener("click", importPackage);
  $("#load-file-btn").addEventListener("click", async () => {
    const file = $("#file-input").files[0];
    if (!file) return;
    $("#package-json").value = await file.text();
  });
  document.querySelectorAll("[data-sample]").forEach((btn) => {
    btn.addEventListener("click", () => loadSample(btn.dataset.sample));
  });
  $("#reset-btn").addEventListener("click", resetDemo);
  $("#box-form").addEventListener("submit", queryBox);
  $("#history-form").addEventListener("submit", queryHistory);
  $("#cell-form").addEventListener("submit", queryCell);
  $("#package-json").addEventListener("paste", () => setTimeout(() => { $("#import-result").hidden = true; }, 0));

  loadSample("v3.json").catch(() => {});
  refreshAll().catch(() => {});
  queryBox().catch(() => {});
}

document.addEventListener("DOMContentLoaded", wire);
