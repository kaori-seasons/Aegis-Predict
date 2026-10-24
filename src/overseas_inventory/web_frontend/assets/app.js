const fmt = new Intl.NumberFormat("zh-CN");
let currentRun = null;
let authenticated = false;
let displayCatalog = { skus: {}, warehouses: {} };
let simulationTimer = null;
let uiBindings = { jobId: null, scenarioBound: false, pageReady: false };

const DEFAULT_SKU_NAMES = {
  "CAB-C1-BLK": "USB-C 快充线 1 米黑色",
  "CAB-C2-BLK": "USB-C 快充线 2 米黑色",
  "CAB-C1-WHT": "USB-C 快充线 1 米白色",
  "STAND-DESK": "手机桌面支架",
  "STAND-CAR": "手机车载支架",
  "STAND-MAG": "磁吸手机支架",
  "APCASE-PRO-CLR": "AirPods Pro 透明保护壳",
  "APCASE-PRO-SIL": "AirPods Pro 硅胶保护壳",
  "APCASE-GEN3": "AirPods 3 代保护壳",
  "IPAD10-CASE": "iPad 10 代保护壳",
  "IPADAIR-CASE": "iPad Air 保护壳",
  "HUB-5IN1": "USB-C 拓展坞 5 合 1",
  "HUB-7IN1": "USB-C 拓展坞 7 合 1",
  "LAPSTAND-FOLD": "折叠笔记本支架",
  "LAPSTAND-LIFT": "升降笔记本支架",
};

const DEFAULT_WAREHOUSE_NAMES = {
  "US-LAX": "美国洛杉矶仓",
  "DE-FRA": "德国法兰克福仓",
  "UK-BHX": "英国伯明翰仓",
  "AU-SYD": "澳洲悉尼仓",
};

const COUNTRY_NAMES = {
  US: "美国",
  DE: "德国",
  UK: "英国",
  AU: "澳洲",
};

const REGION_NAMES = {
  "North America": "北美",
  Europe: "欧洲",
  Oceania: "大洋洲",
};

const el = (id) => document.getElementById(id);

function setDisplayCatalog(plan = {}) {
  displayCatalog = {
    skus: { ...DEFAULT_SKU_NAMES, ...(plan.display_names?.skus || {}) },
    warehouses: { ...DEFAULT_WAREHOUSE_NAMES, ...(plan.display_names?.warehouses || {}) },
  };
}

function skuName(sku) {
  return displayCatalog.skus[sku] || sku;
}

function warehouseName(warehouseId) {
  return displayCatalog.warehouses[warehouseId] || warehouseId;
}

function countryRegion(row) {
  return `${COUNTRY_NAMES[row.country] || row.country} / ${REGION_NAMES[row.region] || row.region}`;
}

function showMessage(text, isError = true) {
  const box = el("message");
  box.textContent = text;
  box.style.color = isError ? "var(--red)" : "var(--green)";
  box.classList.toggle("hidden", !text);
}

function showLoginMessage(text) {
  el("login-message").textContent = text || "";
}

function setAuthenticated(value) {
  authenticated = Boolean(value);
  el("login-panel").classList.toggle("hidden", authenticated);
  el("app-shell").classList.toggle("hidden", !authenticated);
  el("logout-button").classList.toggle("hidden", !authenticated);
  if (!authenticated) {
    setSimulationTimer("", false, "normal");
    currentRun = null;
    uiBindings = { jobId: null, scenarioBound: false, pageReady: false };
    el("workspace").classList.add("hidden");
    el("runs").innerHTML = "";
  }
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && !url.includes("/student/session") && !url.includes("/student/login")) {
    setAuthenticated(false);
  }
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data;
}

async function loadRuns() {
  const data = await requestJson("/student/runs");
  const box = el("runs");
  box.innerHTML = data.runs.length
    ? data.runs.map((run) => `
      <div class="run-item">
        <div><strong>${run.job_id}</strong><span>${run.data_source || "simulation-engine"} / ${run.created_at || ""}</span></div>
        <button type="button" data-open-run="${run.job_id}">打开</button>
      </div>
    `).join("")
    : `<div class="run-item"><div><strong>暂无实验</strong><span>点击“新建仿真实验”生成</span></div></div>`;
  box.querySelectorAll("[data-open-run]").forEach((button) => {
    button.addEventListener("click", () => openRun(button.dataset.openRun));
  });
}

async function loadLiveDemo() {
  showMessage("");
  currentRun = await requestJson("/student/simulation/demo", { method: "POST" });
  renderRun(currentRun);
  activatePage("overview-page");
}

async function openRun(jobId) {
  showMessage("");
  currentRun = await requestJson(`/student/runs/${jobId}`);
  renderRun(currentRun);
}

function metric(label, value, hint = "") {
  return `<div class="metric-card"><span>${label}</span><strong>${value}</strong>${hint ? `<em>${hint}</em>` : ""}</div>`;
}

function statusClass(status) {
  if (status === "批准") return "approved";
  if (status === "驳回") return "rejected";
  return "pending";
}

function riskClassFromValue(value) {
  if (value >= 0.65) return "risk-high";
  if (value >= 0.35) return "risk-mid";
  return "risk-low";
}

function barChart(rows, formatter = (value) => fmt.format(value)) {
  const max = Math.max(1, ...rows.map((row) => Number(row.value) || 0));
  return rows.length ? rows.map((row) => {
    const value = Number(row.value) || 0;
    const width = Math.max(3, Math.min(100, value / max * 100));
    return `
      <div class="bar-row">
        <div class="bar-label">${row.label}</div>
        <div class="bar-track"><i style="width:${width}%; background:${row.color || "var(--charcoal)"}"></i></div>
        <div class="bar-value">${formatter(value)}</div>
      </div>
    `;
  }).join("") : `<div class="event-item">暂无数据</div>`;
}

function svgLinePath(values, width, height, padding, minValue = null, maxValue = null) {
  if (!values.length) return "";
  const min = minValue ?? Math.min(...values);
  const max = maxValue ?? Math.max(...values);
  const span = Math.max(1e-6, max - min);
  return values.map((value, index) => {
    const x = padding + (values.length === 1 ? 0 : index / (values.length - 1) * (width - padding * 2));
    const y = height - padding - ((Number(value) - min) / span * (height - padding * 2));
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
}

function svgAreaPath(values, width, height, padding, minValue = null, maxValue = null) {
  if (!values.length) return "";
  const line = svgLinePath(values, width, height, padding, minValue, maxValue);
  const lastX = padding + (values.length === 1 ? 0 : (width - padding * 2));
  const baseY = height - padding;
  return `${line} L${lastX.toFixed(1)} ${baseY} L${padding} ${baseY} Z`;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function riskColor(value) {
  if (value >= 0.65) return "var(--hot)";
  if (value >= 0.35) return "var(--amber)";
  return "var(--green)";
}

function transportColor(mode) {
  if (mode === "AIR") return "var(--hot)";
  if (mode === "SEA") return "var(--teal)";
  return "var(--charcoal)";
}

const MAP_VIEW = {
  width: 920,
  height: 390,
  padX: 20,
  padY: 24,
};

const SHENZHEN_HUB = {
  latitude: 22.5431,
  longitude: 114.0579,
};

function geoX(longitude) {
  return MAP_VIEW.padX + ((Number(longitude) + 180) / 360) * (MAP_VIEW.width - MAP_VIEW.padX * 2);
}

function geoY(latitude) {
  return MAP_VIEW.padY + ((84 - Number(latitude)) / 168) * (MAP_VIEW.height - MAP_VIEW.padY * 2);
}

function geoPoint(row) {
  return {
    x: clamp(geoX(row.longitude), MAP_VIEW.padX, MAP_VIEW.width - MAP_VIEW.padX),
    y: clamp(geoY(row.latitude), MAP_VIEW.padY, MAP_VIEW.height - MAP_VIEW.padY),
  };
}

function mapPoint(row) {
  const point = geoPoint(row);
  const cardPositions = {
    "US-LAX": { cardX: 48, cardY: 202 },
    "UK-BHX": { cardX: 302, cardY: 78 },
    "DE-FRA": { cardX: 512, cardY: 122 },
    "AU-SYD": { cardX: 692, cardY: 292 },
  };
  return {
    ...point,
    ...(cardPositions[row.warehouse_id] || {
      cardX: clamp(point.x + 18, 24, MAP_VIEW.width - 200),
      cardY: clamp(point.y - 58, 22, MAP_VIEW.height - 98),
    }),
  };
}

function renderNetworkMap(plan) {
  const warehouses = plan.warehouse_map || [];
  const maxInbound = Math.max(1, ...warehouses.map((row) => row.planned_inbound || 0));
  const source = geoPoint(SHENZHEN_HUB);
  const totalInbound = warehouses.reduce((sum, row) => sum + (row.planned_inbound || 0), 0);
  const avgRisk = warehouses.length
    ? warehouses.reduce((sum, row) => sum + Number(row.risk || 0), 0) / warehouses.length
    : 0;
  const meridians = [-120, -60, 0, 60, 120];
  const parallels = [-40, 0, 40, 80];
  const paths = warehouses.map((row, index) => {
    const point = mapPoint(row);
    const color = warehouseStatusColor(row);
    const width = 1.25 + (row.planned_inbound || 0) / maxInbound * 3.2;
    const curveLift = row.warehouse_id === "AU-SYD" ? 66 : row.warehouse_id === "US-LAX" ? -72 : -42 - index * 7;
    const controlA = { x: source.x - 70, y: source.y + curveLift };
    const controlB = { x: point.x + (source.x - point.x) * 0.35, y: point.y + curveLift };
    const path = `M ${source.x} ${source.y} C ${controlA.x} ${controlA.y}, ${controlB.x} ${controlB.y}, ${point.x} ${point.y}`;
    return `
      <path d="${path}" class="route-glow" stroke="${color}" stroke-width="${(width + 2.6).toFixed(1)}"/>
      <path d="${path}" class="route-line" stroke="${color}" stroke-width="${width.toFixed(1)}" marker-end="url(#arrow-${index})"/>
      <path d="${path}" class="route-pulse" stroke="${color}" stroke-width="${Math.max(1, width - .55).toFixed(1)}"/>
      <marker id="arrow-${index}" markerWidth="4.8" markerHeight="4.8" refX="4.2" refY="1.8" orient="auto">
        <path d="M0,0 L4.4,1.8 L0,3.6 Z" fill="${color}"/>
      </marker>
    `;
  }).join("");
  const nodes = warehouses.map((row) => {
    const point = mapPoint(row);
    const color = warehouseStatusColor(row);
    const radius = 5.5 + (row.planned_inbound || 0) / maxInbound * 5;
    const usage = Math.round((row.usage_rate || 0) * 100);
    const risk = Math.round((row.risk || 0) * 100);
    const cardWidth = 148;
    const cardHeight = 58;
    const leaderStartX = point.x + (point.cardX > point.x ? radius + 3 : -radius - 3);
    const leaderStartY = point.y;
    const leaderEndX = clamp(point.cardX + cardWidth / 2, 34, MAP_VIEW.width - 34);
    const leaderEndY = clamp(point.cardY + cardHeight / 2, 30, MAP_VIEW.height - 30);
    return `
      <g class="warehouse-node">
        <path d="M ${leaderStartX.toFixed(1)} ${leaderStartY.toFixed(1)} L ${leaderEndX.toFixed(1)} ${leaderEndY.toFixed(1)}" class="label-leader"/>
        <circle cx="${point.x}" cy="${point.y}" r="${radius + 7}" fill="${color}" opacity=".12"/>
        <circle cx="${point.x}" cy="${point.y}" r="${radius + 3}" fill="none" stroke="${color}" stroke-width="1.5" opacity=".72"/>
        <circle cx="${point.x}" cy="${point.y}" r="${radius}" fill="#fdfdf8" stroke="${color}" stroke-width="2.8"/>
        <circle cx="${point.x}" cy="${point.y}" r="2.8" fill="${color}"/>
        <g transform="translate(${point.cardX}, ${point.cardY})">
          <rect width="${cardWidth}" height="${cardHeight}" rx="8" class="warehouse-card-bg"/>
          <rect x="0" y="0" width="4" height="${cardHeight}" rx="2" fill="${color}"/>
          <text x="12" y="18" class="warehouse-code">${warehouseName(row.warehouse_id)}</text>
          <text x="12" y="33" class="warehouse-country">${countryRegion(row)}</text>
          <text x="12" y="49" class="warehouse-metric">${row.inbound_units > 0 ? "在途" : "入仓"} ${fmt.format(row.planned_inbound)} 件</text>
          <text x="94" y="49" class="warehouse-metric">仓容 ${usage}%</text>
          <text x="122" y="18" class="warehouse-risk">R${risk}</text>
        </g>
      </g>
    `;
  }).join("");
  el("network-map").innerHTML = `
    <svg viewBox="0 0 920 390" role="img" aria-label="global warehouse network">
      <defs>
        <linearGradient id="seaGradient" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#12160f"/>
          <stop offset="48%" stop-color="#1a2014"/>
          <stop offset="100%" stop-color="#0f130c"/>
        </linearGradient>
        <radialGradient id="mapGlow" cx="78%" cy="40%" r="74%">
          <stop offset="0%" stop-color="#f54e00" stop-opacity=".18"/>
          <stop offset="45%" stop-color="#3f6b4a" stop-opacity=".14"/>
          <stop offset="100%" stop-color="#12160f" stop-opacity="0"/>
        </radialGradient>
        <radialGradient id="hubGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#fdfdf8" stop-opacity=".95"/>
          <stop offset="42%" stop-color="#f54e00" stop-opacity=".24"/>
          <stop offset="100%" stop-color="#f54e00" stop-opacity="0"/>
        </radialGradient>
        <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="12" stdDeviation="10" flood-color="#0a0c08" flood-opacity=".28"/>
        </filter>
        <filter id="landShadow" x="-6%" y="-8%" width="112%" height="116%">
          <feDropShadow dx="0" dy="5" stdDeviation="4" flood-color="#020a12" flood-opacity=".34"/>
        </filter>
      </defs>
      <rect x="0" y="0" width="920" height="390" fill="url(#seaGradient)"/>
      <rect x="0" y="0" width="920" height="390" fill="url(#mapGlow)"/>
      <g class="map-graticule">
        ${meridians.map((lon) => `<line x1="${geoX(lon)}" y1="18" x2="${geoX(lon)}" y2="372"/>`).join("")}
        ${parallels.map((lat) => `<line x1="18" y1="${geoY(lat)}" x2="902" y2="${geoY(lat)}"/>`).join("")}
        ${meridians.map((lon) => `<text x="${geoX(lon)}" y="374" text-anchor="middle">${lon > 0 ? lon + "E" : Math.abs(lon) + "W"}</text>`).join("")}
      </g>
      <image href="/assets/world-land.svg" x="0" y="0" width="920" height="390" class="world-land-image"/>
      <rect x="14" y="14" width="892" height="362" rx="18" class="map-frame"/>
      <text x="32" y="36" class="map-title">WORLD WAREHOUSE NETWORK</text>
      <text x="32" y="58" class="map-subtle">真实世界地图 / 3C 配件库存池供货全球</text>
      <g class="map-stats" transform="translate(724, 28)">
        <rect width="150" height="46" rx="9"/>
        <text x="12" y="20" class="map-stat-label">入仓/在途</text>
        <text x="12" y="37" class="map-stat-value">${fmt.format(totalInbound)} 件</text>
        <text x="92" y="20" class="map-stat-label">风险</text>
        <text x="92" y="37" class="map-stat-value">${Math.round(avgRisk * 100)}%</text>
      </g>
      ${paths}
      <g class="source-node">
        <circle cx="${source.x}" cy="${source.y}" r="34" fill="url(#hubGlow)"/>
        <circle cx="${source.x}" cy="${source.y}" r="17" fill="#fdfdf8" stroke="#f54e00" stroke-width="3"/>
        <circle cx="${source.x}" cy="${source.y}" r="4.5" fill="#f54e00"/>
        <text x="${source.x}" y="${source.y + 35}" text-anchor="middle" class="source-title">深圳中心库存池</text>
        <text x="${source.x}" y="${source.y + 50}" text-anchor="middle" class="source-sub">中心供应池</text>
      </g>
      ${nodes}
      <g class="map-legend" transform="translate(32, 335)">
        <rect width="430" height="34" rx="10"/>
        <circle cx="20" cy="17" r="6" fill="var(--green)"/><text x="34" y="21">低风险</text>
        <circle cx="106" cy="17" r="6" fill="var(--amber)"/><text x="120" y="21">需观察</text>
        <circle cx="198" cy="17" r="6" fill="var(--red)"/><text x="212" y="21">高风险</text>
        <circle cx="284" cy="17" r="6" fill="var(--charcoal)"/><text x="298" y="21">积压</text>
        <path d="M348 17 L388 17" stroke="#71a7ff" stroke-width="3.5" stroke-linecap="round"/><text x="398" y="21">入仓量</text>
      </g>
    </svg>
  `;
}

function warehouseStatusColor(row) {
  const risk = Number(row.risk || 0);
  const usage = Number(row.usage_rate || 0);
  if (risk >= 0.65) return "#f54e00"; // hot exception
  if (usage >= 0.9 && risk < 0.35) return "#1e1f23"; // backlog / piled inventory
  if (risk >= 0.35) return "#9a5600"; // watch
  return "#2f6b3a"; // healthy
}

function renderPoolFlow(plan) {
  const levels = plan.pool_levels || [];
  const totals = levels.reduce((acc, row) => {
    acc.allocated += row.allocated_units || 0;
    acc.remaining += row.remaining_units || 0;
    acc.reserved += row.reserved_units || 0;
    return acc;
  }, { allocated: 0, remaining: 0, reserved: 0 });
  const total = Math.max(1, totals.allocated + totals.remaining + totals.reserved);
  const top = [...levels].sort((a, b) => b.waterline - a.waterline).slice(0, 5);
  const meter = (row) => {
    const rowTotal = Math.max(1, row.allocated_units + row.remaining_units + row.reserved_units);
    return `
      <div class="pool-meter">
        <strong><span>${skuName(row.sku)}</span><span>${Math.round(row.waterline * 100)}%</span></strong>
        <div class="stack-track">
          <i class="allocated" style="width:${row.allocated_units / rowTotal * 100}%"></i>
          <i class="remaining" style="width:${row.remaining_units / rowTotal * 100}%"></i>
          <i class="reserved" style="width:${row.reserved_units / rowTotal * 100}%"></i>
        </div>
      </div>
    `;
  };
  el("pool-flow").innerHTML = `
    <div class="pool-meter">
      <strong><span>总库存池</span><span>${fmt.format(total)} 件</span></strong>
      <div class="stack-track">
        <i class="allocated" style="width:${totals.allocated / total * 100}%"></i>
        <i class="remaining" style="width:${totals.remaining / total * 100}%"></i>
        <i class="reserved" style="width:${totals.reserved / total * 100}%"></i>
      </div>
    </div>
    <div class="flow-legend">
      <span>已分配</span><span class="remaining">剩余</span><span class="reserved">保留</span>
    </div>
    ${top.map(meter).join("")}
  `;
}

function renderModelSummary(plan, recommendations) {
  const pipeline = plan.model_pipeline || {};
  const modelName = pipeline.active_model || recommendations[0]?.model_name || "ML ensemble";
  const sampleCount = pipeline.training_samples || 0;
  const avgConfidence = recommendations.length
    ? recommendations.reduce((sum, row) => sum + (1 - Number(row.urgency || 0)), 0) / recommendations.length
    : 0;
  el("model-summary").innerHTML = `
    <div class="model-pill"><span>当前预测模型</span><strong>${modelName}</strong></div>
    <div class="mini-stat"><span>训练样本</span><strong>${fmt.format(sampleCount)}</strong></div>
    <div class="mini-stat"><span>市场信号</span><strong>${fmt.format(pipeline.signal_count || 0)}</strong></div>
    <div class="mini-stat"><span>稳定度</span><strong>${Math.round(avgConfidence * 100)}%</strong></div>
  `;
}

function renderForecastVisual(rows, period = 30) {
  const max = Math.max(1, ...rows.map((row) => row.p95_units || 0));
  el("forecast-visual").innerHTML = `
    <div class="forecast-warehouse-matrix">
      <div class="forecast-matrix-title">
        <div>
          <span class="section-title">各仓对比</span>
          <strong>${period} 天需求、库存覆盖与风险</strong>
        </div>
        <em>基于当前模拟订单流滚动计算</em>
      </div>
      ${rows.map((row) => {
        const p50 = Math.round(Number(row.daily_rate || 0) * period);
        const p80 = scaleForecast(row.p80_units, period);
        const p95 = scaleForecast(row.p95_units, period);
        const inventory = Number(row.inventory_position || 0);
        const coverage = Math.round(inventory / Math.max(1, Number(row.daily_rate || 0)));
        const urgency = Number(row.urgency || 0);
        const riskTone = urgency >= 0.65 ? "high" : urgency >= 0.35 ? "mid" : "low";
        const riskText = urgency >= 0.65 ? "高风险" : urgency >= 0.35 ? "需观察" : "稳定";
        const width = Math.max(6, Number(row.p95_units || 0) / max * 100);
        return `
          <div class="forecast-matrix-row ${riskTone}">
            <div>
              <strong>${warehouseName(row.warehouse_id)}</strong>
              <span>日均 ${row.daily_rate} 件</span>
            </div>
            <div><span>P50</span><b>${fmt.format(p50)}</b></div>
            <div><span>P80</span><b>${fmt.format(p80)}</b></div>
            <div><span>P95</span><b>${fmt.format(p95)}</b></div>
            <div><span>当前库存</span><b>${fmt.format(inventory)}</b></div>
            <div><span>可售天数</span><b>${fmt.format(coverage)} 天</b></div>
            <i>${riskText}</i>
            <div class="forecast-intensity"><span style="width:${width}%"></span></div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderUrgencyMatrix(plan) {
  const recommendations = plan.recommendations || [];
  const warehouses = [...new Set(recommendations.map((row) => row.warehouse_id))].sort();
  const categories = [...new Set(recommendations.map((row) => categoryFromSku(row.sku)))];
  const valueFor = (category, warehouse) => {
    const rows = recommendations.filter((row) => categoryFromSku(row.sku) === category && row.warehouse_id === warehouse);
    return rows.length ? Math.max(...rows.map((row) => Number(row.urgency) || 0)) : 0;
  };
  const header = `<div class="matrix-row"><span></span>${warehouses.map((id) => `<span class="matrix-head">${warehouseName(id)}</span>`).join("")}</div>`;
  const body = categories.map((category) => `
    <div class="matrix-row">
      <span class="matrix-head">${category}</span>
      ${warehouses.map((id) => {
        const value = valueFor(category, id);
        return `<span class="matrix-cell" style="background:${riskColor(value)}; opacity:${0.34 + value * 0.66}">${value.toFixed(2)}</span>`;
      }).join("")}
    </div>
  `).join("");
  el("urgency-matrix").innerHTML = header + body;
}

function renderApprovalFunnel(approvals) {
  const counts = {
    pending: approvals.filter((row) => row.status === "待审批").length,
    approved: approvals.filter((row) => row.status === "批准").length,
    rejected: approvals.filter((row) => row.status === "驳回").length,
  };
  const max = Math.max(1, counts.pending, counts.approved, counts.rejected);
  const row = (key, label) => `
    <div class="funnel-row ${key}">
      <strong><span>${label}</span><span>${counts[key]}</span></strong>
      <div class="funnel-track"><i style="width:${Math.max(4, counts[key] / max * 100)}%"></i></div>
    </div>
  `;
  el("approval-funnel").innerHTML = row("pending", "待审批") + row("approved", "已批准") + row("rejected", "已驳回");
}

function renderAllocationFlow(plan) {
  const warehouses = [...new Set((plan.allocations || []).map((row) => row.warehouse_id))].sort();
  const maxAllocated = Math.max(1, ...warehouses.map((id) => (
    plan.allocations || []
  ).filter((row) => row.warehouse_id === id).reduce((sum, row) => sum + row.allocated_units, 0)));
  const rows = warehouses.map((id, index) => {
    const related = (plan.allocations || []).filter((row) => row.warehouse_id === id);
    const allocated = related.reduce((sum, row) => sum + row.allocated_units, 0);
    const modes = [...new Set(related.flatMap((row) => String(row.transport_mode).split("+")))].filter(Boolean);
    const share = allocated / maxAllocated;
    const color = transportColor(modes.includes("AIR") ? "AIR" : modes[0]);
    const primaryMode = modes.includes("AIR") ? "AIR" : modes[0] || "SEA";
    return `
      <div class="allocation-route" style="--route-color:${color}; --route-width:${Math.max(8, Math.round(share * 100))}%">
        <div class="route-rank">${index + 1}</div>
        <div class="route-main">
          <div class="route-meta">
            <strong>${warehouseName(id)}</strong>
            <span>${transportModeLabel(primaryMode)} · ${fmt.format(allocated)} 件</span>
          </div>
          <div class="route-band"><i></i></div>
        </div>
        <b>${Math.round(share * 100)}%</b>
      </div>
    `;
  }).join("");
  el("allocation-flow").innerHTML = `
    <section class="allocation-flow-board">
      <div class="flow-source-card">
        <span>来源库存池</span>
        <strong>深圳中心仓</strong>
        <em>按需求、缺口、仓容和运输成本拆分</em>
      </div>
      <div class="route-list">
      ${rows}
      </div>
    </section>
  `;
}

function renderEvaluationScoreboard(plan) {
  const sim = plan.simulation_comparison;
  const metricTile = (label, baseline, intelligent, suffix = "") => {
    const max = Math.max(1, Number(baseline), Number(intelligent));
    return `
      <div class="score-tile">
        <span>${label}</span>
        <strong>${intelligent}${suffix}</strong>
        <div class="score-line"><em>传统</em><div class="score-track"><i style="width:${Number(baseline) / max * 100}%; background:#9aa7b7"></i></div><b>${baseline}${suffix}</b></div>
        <div class="score-line"><em>智能</em><div class="score-track"><i style="width:${Number(intelligent) / max * 100}%"></i></div><b>${intelligent}${suffix}</b></div>
      </div>
    `;
  };
  el("evaluation-scoreboard").innerHTML = [
    metricTile("订单满足率", percentValue(sim.baseline.fill_rate), percentValue(sim.intelligent.fill_rate), "%"),
    metricTile("缺货率", percentValue(sim.baseline.stockout_rate), percentValue(sim.intelligent.stockout_rate), "%"),
    metricTile("平均配送", sim.baseline.average_delivery_days, sim.intelligent.average_delivery_days, "天"),
    metricTile("周转天数", sim.baseline.inventory_turnover_days, sim.intelligent.inventory_turnover_days, "天"),
    metricTile("总利润", Math.round(sim.baseline.total_profit), Math.round(sim.intelligent.total_profit)),
  ].join("");
}

function dashboardMetrics(plan, state) {
  const simulation = state?.simulation || {};
  const inventoryValues = Object.values(simulation.inventory || {});
  const fallbackInventory = (plan.recommendations || []).reduce((sum, row) => sum + Number(row.inventory_position || 0), 0);
  const totalInventory = inventoryValues.length
    ? inventoryValues.reduce((sum, value) => sum + Number(value || 0), 0)
    : fallbackInventory;
  const demand30d = Math.round((plan.recommendations || []).reduce((sum, row) => sum + Number(row.p80_units || 0), 0));
  const dailyDemand = (plan.recommendations || []).reduce((sum, row) => sum + Number(row.daily_rate || 0), 0);
  const riskSkuCount = (plan.recommendations || []).filter((row) => Number(row.urgency || 0) >= 0.55).length;
  const fillRate = simulation.units_requested
    ? Math.round(Number(simulation.fill_rate || 0) * 1000) / 10
    : Math.round(Number(plan.simulation_comparison?.intelligent?.fill_rate || 0.946) * 1000) / 10;
  return {
    totalInventory,
    riskSkuCount,
    demand30d,
    fillRate,
    turnoverDays: Math.round(totalInventory / Math.max(1, dailyDemand)),
    todayReplenishment: (plan.recommendations || []).reduce((sum, row) => sum + Number(row.recommended_units || 0), 0),
  };
}

function topRiskSnapshot(plan, state) {
  const simulation = state?.simulation || {};
  const inventory = simulation.inventory || {};
  const rows = (plan.recommendations || []).map((row) => {
    const key = `${row.sku}||${row.warehouse_id}`;
    const currentInventory = Number(inventory[key] ?? row.inventory_position ?? 0);
    const daysOfSupply = currentInventory / Math.max(1, Number(row.daily_rate || 0));
    const risk = simulation_risk_label(daysOfSupply, row.urgency);
    return { ...row, currentInventory, daysOfSupply, riskScore: risk.score };
  }).sort((a, b) => b.riskScore - a.riskScore || b.recommended_units - a.recommended_units);
  return rows[0] || null;
}

function storyStep(index, label, value, detail) {
  return `
    <article class="story-step">
      <span>${index}. ${label}</span>
      <strong>${value}</strong>
      <em>${detail}</em>
    </article>
  `;
}

function renderStoryStrip(plan, state, dashboard) {
  const box = el("story-strip");
  if (!box) return;
  const risk = topRiskSnapshot(plan, state);
  const topRecommendation = [...(plan.recommendations || [])]
    .sort((a, b) => Number(b.recommended_units || 0) - Number(a.recommended_units || 0))[0];
  const allocationRows = allocationFocusRows(plan);
  const allocationTop = allocationRows[0];
  const comparison = plan.simulation_comparison || {};
  const baselineFill = Number(comparison.baseline?.fill_rate || 0);
  const intelligentFill = Number(comparison.intelligent?.fill_rate || 0);
  const fillLift = Math.max(0, Math.round((intelligentFill - baselineFill) * 1000) / 10);
  const running = Boolean(state?.simulation?.running);
  const clock = state?.simulation?.simulation_clock || simulationTime(state?.simulation?.simulated_minutes || 0, state?.simulation?.history_days || 180);
  const riskText = risk
    ? `${skuName(risk.sku)} / ${warehouseName(risk.warehouse_id)}`
    : "暂无高风险 SKU";
  const riskDetail = risk
    ? `预计可售 ${risk.daysOfSupply.toFixed(1)} 天`
    : "库存仍处于健康区间";
  const decisionText = topRecommendation
    ? `${warehouseName(topRecommendation.warehouse_id)} 补 ${fmt.format(topRecommendation.recommended_units)} 件`
    : "暂无补货建议";
  const allocationText = allocationTop
    ? `${warehouseName(allocationTop.warehouse_id)} 分 ${fmt.format(allocationTop.allocated_units)} 件`
    : "暂无分仓方案";

  box.innerHTML = [
    storyStep(1, "先盯风险", riskText, riskDetail),
    storyStep(2, "再算需求", `${fmt.format(dashboard.demand30d)} 件`, "未来 30 天预测需求"),
    storyStep(3, "开补货单", decisionText, `今日建议合计 ${fmt.format(dashboard.todayReplenishment)} 件`),
    storyStep(4, "池里分货", allocationText, allocationTop ? `核心 SKU：${skuName(allocationTop.focusSku)}` : "等待优化器输出"),
    storyStep(5, "对打验收", `${dashboard.fillRate}% 满足率`, `较传统方案提升 ${fillLift} 个点 / ${running ? "运行中" : "暂停"} ${clock}`),
  ].join("");
}

function renderRun(data, options = {}) {
  const mode = options.mode || "full";
  const { metadata, plan, state } = data;
  setDisplayCatalog(plan);
  const simulation = state.simulation || {};
  const jobChanged = uiBindings.jobId !== metadata.job_id;
  const forceFull = mode === "full" || jobChanged || !uiBindings.pageReady;

  el("workspace").classList.remove("hidden");
  if (forceFull) {
    el("job-id").textContent = `Run ${metadata.job_id}`;
    el("run-title").textContent = document.body.classList.contains("simulation-only")
      ? "海外仓补货工作台"
      : metadata.data_source || "海外仓仿真实验结果";
    el("file-actions").innerHTML = `
      <a href="/student/runs/${metadata.job_id}/files/dashboard.html" target="_blank" rel="noreferrer">运营看板</a>
      <a href="/student/runs/${metadata.job_id}/files/replenishment_plan.json" target="_blank" rel="noreferrer">JSON</a>
      <a href="/student/runs/${metadata.job_id}/files/replenishment_plan.md" target="_blank" rel="noreferrer">报告</a>
    `;
  }

  renderLiveDashboard(plan, state, metadata.job_id, { bindControls: forceFull || !uiBindings.scenarioBound });

  if (forceFull) {
    renderForecast(plan);
    renderReplenishment(metadata.job_id, plan, state);
    renderAllocation(metadata.job_id, plan, state);
    renderEvaluation(plan);
    uiBindings.jobId = metadata.job_id;
    uiBindings.pageReady = true;
  } else {
    // Live ticks only refresh inventory-sensitive operational views.
    renderReplenishmentLive(plan, state);
    renderEvaluationLive(plan, state);
  }

  // Always keep the simulation timer aligned with the latest run state.
  setSimulationTimer(metadata.job_id, Boolean(simulation.running), simulation.scenario || "normal");
}

function renderLiveDashboard(plan, state, jobId, options = {}) {
  const dashboard = dashboardMetrics(plan, state);
  const simulation = state.simulation || {};
  const requested = Number(simulation.units_requested || 0);
  const stockout = Number(simulation.units_stockout || 0);
  const liveFill = requested
    ? Math.round((1 - stockout / Math.max(1, requested)) * 1000) / 10
    : dashboard.fillRate;
  const topRisk = topRiskSnapshot(plan, state);
  const riskHint = topRisk
    ? `${skuName(topRisk.sku)} · ${warehouseName(topRisk.warehouse_id)}`
    : "暂无高风险项";

  renderStoryStrip(plan, state, dashboard);
  el("metrics").innerHTML = [
    metric("全球总库存", `${fmt.format(dashboard.totalInventory)} 件`, simulation.running ? "实时扣减中" : "基线水位"),
    metric("缺货风险 SKU", `${fmt.format(dashboard.riskSkuCount)} 个`, riskHint),
    metric("预计 30 天需求", `${fmt.format(dashboard.demand30d)} 件`, "P80 合计"),
    metric("实时履约率", `${liveFill}%`, requested ? `需求 ${fmt.format(requested)} 件` : "等待订单"),
    metric("库存周转天数", `${dashboard.turnoverDays} 天`, "库存 / 日均需求"),
    metric("今日建议补货", `${fmt.format(dashboard.todayReplenishment)} 件`, "计划层输出"),
  ].join("");

  renderOverview(plan, state, { liveOnly: !options.bindControls && uiBindings.pageReady });
  renderSimulation(jobId, simulation, { bindControls: Boolean(options.bindControls) });
}

function renderReplenishmentLive(plan, state) {
  const simulation = state.simulation || {};
  const inventory = simulation.inventory || {};
  const inbound = {};
  (simulation.in_transit_shipments || []).forEach((item) => {
    const key = `${item.sku}||${item.destination_warehouse_id}`;
    inbound[key] = (inbound[key] || 0) + Number(item.quantity || 0);
  });
  const allocationByKey = Object.fromEntries((plan.allocations || []).map((row) => [`${row.sku}||${row.warehouse_id}`, row]));
  el("replenishments").innerHTML = (plan.recommendations || [])
    .map((row) => renderReplenishmentRow(row, inventory, inbound, allocationByKey))
    .join("");
}

function renderEvaluationLive(plan, state) {
  // Scoreboard stays static; only the live simulation strip under evaluation updates via renderSimulation.
  void plan;
  void state;
}

function simulationWarehouseRows(plan, state) {
  const simulation = state?.simulation || {};
  const metrics = simulation.warehouse_metrics || [];
  if (!metrics.length) return plan.warehouse_map || [];
  const metricByWarehouse = Object.fromEntries(metrics.map((row) => [row.warehouse_id, row]));
  return (plan.warehouse_map || []).map((row) => {
    const metric = metricByWarehouse[row.warehouse_id];
    if (!metric) return row;
    const liveInbound = Number(metric.inbound_units || 0);
    const basePosition = Math.max(1, Number(row.current_position || 1));
    const liveUsageRate = clamp(Number(row.usage_rate || 0) * Number(metric.current_inventory || 0) / basePosition, 0, 1.2);
    return {
      ...row,
      current_position: metric.current_inventory,
      planned_inbound: liveInbound > 0 ? liveInbound : row.planned_inbound,
      inbound_units: liveInbound,
      days_of_supply: metric.days_of_supply,
      usage_rate: liveUsageRate,
      risk: metric.risk,
    };
  });
}

function renderOverview(plan, state, options = {}) {
  const warehouseRows = simulationWarehouseRows(plan, state);
  renderNetworkMap({ ...plan, warehouse_map: warehouseRows });
  renderRiskRanking(plan, state);

  const riskRows = [...warehouseRows]
    .sort((a, b) => b.risk - a.risk)
    .map((row) => ({
      label: warehouseName(row.warehouse_id),
      value: row.risk,
      color: row.risk >= 0.65 ? "var(--hot)" : row.risk >= 0.35 ? "var(--amber)" : "var(--green)",
    }));
  el("risk-chart").innerHTML = barChart(riskRows, (value) => value.toFixed(2));

  // Pool / category structure is plan-level and only needs full refreshes.
  if (!options.liveOnly) {
    const poolRows = [...(plan.pool_levels || [])]
      .sort((a, b) => b.waterline - a.waterline)
      .slice(0, 8)
      .map((row) => ({
        label: skuName(row.sku),
        value: row.waterline,
        color: row.waterline >= 0.8 ? "var(--red)" : row.waterline >= 0.5 ? "var(--amber)" : "var(--teal)",
      }));
    el("pool-chart").innerHTML = barChart(poolRows, (value) => `${Math.round(value * 100)}%`);

    const categories = {};
    (plan.recommendations || []).forEach((row) => {
      const prefix = categoryFromSku(row.sku);
      categories[prefix] = (categories[prefix] || 0) + row.recommended_units;
    });
    const categoryRows = Object.entries(categories)
      .sort((a, b) => b[1] - a[1])
      .map(([label, value]) => ({ label, value, color: "var(--charcoal)" }));
    el("category-chart").innerHTML = barChart(categoryRows);
  }
}

function renderRiskRanking(plan, state) {
  const simulation = state?.simulation || {};
  const inventory = simulation.inventory || {};
  const inbound = {};
  (simulation.in_transit_shipments || []).forEach((item) => {
    const key = `${item.sku}||${item.destination_warehouse_id}`;
    inbound[key] = (inbound[key] || 0) + Number(item.quantity || 0);
  });
  const rows = (plan.recommendations || []).map((row) => {
    const key = `${row.sku}||${row.warehouse_id}`;
    const currentInventory = Number(inventory[key] ?? row.inventory_position ?? 0);
    const projectedDaily = Math.max(1, Number(row.daily_rate || 0));
    const daysOfSupply = currentInventory / projectedDaily;
    const risk = simulation_risk_label(daysOfSupply, row.urgency);
    return {
      ...row,
      currentInventory,
      inboundUnits: inbound[key] || 0,
      daysOfSupply,
      riskLabel: risk.label,
      riskScore: risk.score,
    };
  }).sort((a, b) => b.riskScore - a.riskScore || b.recommended_units - a.recommended_units).slice(0, 8);

  el("risk-ranking").innerHTML = rows.map((row, index) => `
    <div class="risk-row ${riskClassFromValue(row.riskScore)}">
      <b>${index + 1}</b>
      <div>
        <strong>${skuName(row.sku)}</strong>
        <span>${warehouseName(row.warehouse_id)} / 当前 ${fmt.format(row.currentInventory)} 件 / 在途 ${fmt.format(row.inboundUnits)} 件</span>
      </div>
      <em>${row.daysOfSupply.toFixed(1)} 天</em>
      <i>${row.riskLabel}</i>
    </div>
  `).join("");
}

function simulation_risk_label(daysOfSupply, urgency) {
  const score = Math.max(Number(urgency || 0), daysOfSupply <= 5 ? 0.8 : daysOfSupply <= 9 ? 0.55 : 0.2);
  if (score >= 0.65) return { label: "高", score };
  if (score >= 0.35) return { label: "中", score };
  return { label: "低", score };
}

function renderSimulation(jobId, simulation, options = {}) {
  const running = Boolean(simulation.running);
  const scenarioSelect = el("sim-scenario");
  if (options.bindControls || !uiBindings.scenarioBound || uiBindings.jobId !== jobId) {
    scenarioSelect.value = simulation.scenario || "normal";
    el("sim-start").onclick = () => startSimulation(jobId, scenarioSelect.value);
    el("sim-stop").onclick = () => stopSimulation(jobId);
    el("sim-reset").onclick = () => resetSimulation(jobId);
    scenarioSelect.onchange = () => {
      if (currentRun?.state?.simulation?.running) startSimulation(jobId, scenarioSelect.value);
    };
    uiBindings.scenarioBound = true;
  } else if (document.activeElement !== scenarioSelect) {
    scenarioSelect.value = simulation.scenario || "normal";
  }

  const requested = Number(simulation.units_requested || 0);
  const fulfilled = Number(simulation.units_fulfilled || 0);
  const stockout = Number(simulation.units_stockout || 0);
  const fillRate = requested ? Math.round(fulfilled / requested * 100) : 100;
  const statusText = running ? "运行中" : "已暂停";
  const clock = simulation.simulation_clock || simulationTime(simulation.simulated_minutes || 0, simulation.history_days || 180);
  const inTransit = simulation.in_transit_shipments || [];
  el("sim-clock").innerHTML = `<span class="pulse-dot ${running ? "" : "paused"}"></span>${statusText} / ${simulation.scenario_label || "常规滚动"} / ${clock}`;
  el("sim-metrics").innerHTML = [
    metric("模拟时间", clock),
    metric("历史基线", `${fmt.format(simulation.history_days || 180)} 天`),
    metric("累计订单", fmt.format(simulation.orders_total || 0)),
    metric("履约率", `${fillRate}%`),
    metric("缺货件数", fmt.format(stockout)),
    metric("取消/退货", `${fmt.format(simulation.units_cancelled || 0)} / ${fmt.format(simulation.units_returned || 0)}`),
    metric("延迟待发", fmt.format((simulation.market_pulse || {}).backlog_units || simulation.units_delayed || 0)),
    metric("模拟毛利", `$${fmt.format(Math.round(simulation.gross_margin || 0))}`),
    metric("在途补货", `${fmt.format(inTransit.length)} 批`),
    metric("滚动预测", `V${simulation.forecast_version || 1} / ${simulation.next_forecast_in_hours || 24}h`),
    metric("补货决策", `V${simulation.decision_version || 1} / ${simulation.next_decision_in_hours || 6}h`),
  ].join("");

  renderMarketPulse(simulation.market_pulse || {});
  renderSimulationCharts(simulation);
  renderSimulationWarehouses(simulation.warehouse_metrics || []);
  renderSimulationOrders(simulation.recent_orders || []);
  renderSimulationForecast(simulation.latest_forecast_summary || []);
  renderSimulationShipments(simulation);
  renderSimulationRecommendations(simulation.dynamic_recommendations || []);
  renderSimulationEvents(simulation);
}

function renderMarketPulse(pulse) {
  const platformMix = pulse.platform_mix || {};
  const capacityLoad = pulse.capacity_load || {};
  const platformLabel = Object.keys(platformMix).length
    ? Object.entries(platformMix).slice(0, 3).map(([name, value]) => `${name} ${value}%`).join(" / ")
    : "等待订单";
  const maxLoad = Object.keys(capacityLoad).length
    ? Math.max(...Object.values(capacityLoad).map((value) => Number(value || 0)))
    : 0;
  el("market-pulse").innerHTML = [
    pulseCard("流量指数", pulse.traffic_index || 100, "随本地时段、平台活动和广告波动"),
    pulseCard("促销强度", `${pulse.promotion_intensity || 1}x`, "黑五、站内活动、爆单场景会抬升"),
    pulseCard("广告指数", `${pulse.ad_spend_index || 1}x`, "广告投入越高，短时订单越密集"),
    pulseCard("价格指数", `${pulse.price_index || 1}x`, "价格越高，价格敏感 SKU 需求越低"),
    pulseCard("平台占比", platformLabel, "Amazon / TikTok Shop / eBay / Shopify"),
    pulseCard("产能负载", `${Math.round(maxLoad * 100)}%`, `待发 ${fmt.format(pulse.backlog_units || 0)} 件，待退 ${fmt.format(pulse.pending_return_units || 0)} 件`),
  ].join("");
}

function pulseCard(label, value, desc) {
  return `
    <div class="pulse-card">
      <span>${label}</span>
      <strong>${value}</strong>
      <em>${desc}</em>
    </div>
  `;
}

function renderSimulationCharts(simulation) {
  renderTrendChart(simulation.time_series || []);
  renderFulfillmentFunnel(simulation);
  renderPlatformMix(simulation.market_pulse || {});
  renderCapacityHeatmap(simulation.warehouse_metrics || [], simulation.market_pulse || {});
}

function renderTrendChart(series) {
  if (!series.length) {
    el("sim-trend-chart").innerHTML = `<div class="event-item empty-state">启动仿真后，这里滚动履约、流量、待发与库存。</div>`;
    return;
  }
  const width = 760;
  const height = 236;
  const padL = 44;
  const padR = 44;
  const padY = 28;
  const recent = series.slice(-48);
  const fill = recent.map((point) => Number(point.fill_rate || 0) * 100);
  const traffic = recent.map((point) => Number(point.traffic_index || 0));
  const backlog = recent.map((point) => Number(point.backlog_units || 0));
  const inventory = recent.map((point) => Number(point.inventory_total || 0));
  const trafficMax = Math.max(120, ...traffic);
  const backlogMax = Math.max(8, ...backlog);
  const inventoryMax = Math.max(1, ...inventory);
  // Normalize traffic/backlog/inventory into chart space with distinct visual weight.
  const trafficNorm = traffic.map((value) => value / trafficMax * 100);
  const backlogNorm = backlog.map((value) => value / backlogMax * 100);
  const inventoryNorm = inventory.map((value) => value / inventoryMax * 100);
  const lineWithPads = (values, minV, maxV) => {
    if (!values.length) return "";
    const span = Math.max(1e-6, maxV - minV);
    return values.map((value, index) => {
      const x = padL + (values.length === 1 ? 0 : index / (values.length - 1) * (width - padL - padR));
      const y = height - padY - ((Number(value) - minV) / span * (height - padY * 2));
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(" ");
  };
  const areaWithPads = (values, minV, maxV) => {
    const line = lineWithPads(values, minV, maxV);
    if (!line) return "";
    const lastX = padL + (values.length === 1 ? 0 : width - padL - padR);
    return `${line} L${lastX.toFixed(1)} ${height - padY} L${padL} ${height - padY} Z`;
  };
  const fillLine = lineWithPads(fill, 0, 100);
  const fillArea = areaWithPads(fill, 0, 100);
  const trafficLine = lineWithPads(trafficNorm, 0, 100);
  const backlogLine = lineWithPads(backlogNorm, 0, 100);
  const inventoryLine = lineWithPads(inventoryNorm, 0, 100);
  const latest = recent[recent.length - 1];
  const yTicks = [100, 75, 50, 25, 0];
  const grid = yTicks.map((tick) => {
    const y = height - padY - (tick / 100 * (height - padY * 2));
    return `
      <line x1="${padL}" y1="${y.toFixed(1)}" x2="${width - padR}" y2="${y.toFixed(1)}" class="trend-gridline"></line>
      <text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="trend-axis">${tick}%</text>
    `;
  }).join("");
  const startClock = recent[0].clock || "";
  const endClock = latest.clock || "";
  el("sim-trend-chart").innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="实时仿真趋势" class="trend-svg">
      <defs>
        <linearGradient id="fillAreaGradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#167457" stop-opacity=".28"/>
          <stop offset="100%" stop-color="#167457" stop-opacity="0"/>
        </linearGradient>
      </defs>
      ${grid}
      <path d="${fillArea}" class="trend-area fill"></path>
      <path d="${inventoryLine}" class="trend-line inventory"></path>
      <path d="${trafficLine}" class="trend-line traffic"></path>
      <path d="${backlogLine}" class="trend-line backlog"></path>
      <path d="${fillLine}" class="trend-line fill"></path>
      <text x="${padL}" y="18" class="trend-title">最近 ${recent.length} 个模拟小时</text>
      <text x="${width - padR}" y="18" text-anchor="end" class="trend-title">${endClock}</text>
      <text x="${padL}" y="${height - 8}" class="trend-axis">${startClock}</text>
      <text x="${width - padR}" y="${height - 8}" text-anchor="end" class="trend-axis">${endClock}</text>
    </svg>
    <div class="trend-legend">
      <span><i class="fill"></i>履约率 ${Math.round(Number(latest.fill_rate || 0) * 100)}%</span>
      <span><i class="traffic"></i>流量 ${fmt.format(latest.traffic_index || 0)}</span>
      <span><i class="backlog"></i>待发 ${fmt.format(latest.backlog_units || 0)}</span>
      <span><i class="inventory"></i>库存 ${fmt.format(latest.inventory_total || 0)} 件</span>
    </div>
  `;
}

function renderFulfillmentFunnel(simulation) {
  const gross = Number(simulation.gross_order_units || simulation.units_requested || 0);
  const requested = Number(simulation.units_requested || 0);
  const fulfilled = Number(simulation.units_fulfilled || 0);
  const abnormal = Number(simulation.units_stockout || 0) + Number(simulation.units_delayed || 0) + Number(simulation.units_cancelled || 0);
  const rows = [
    { label: "下单件数", value: gross, color: "var(--charcoal)" },
    { label: "有效需求", value: requested, color: "var(--green)" },
    { label: "已履约", value: fulfilled, color: "var(--green)" },
    { label: "异常件数", value: abnormal, color: abnormal ? "var(--red)" : "var(--steel)" },
  ];
  const max = Math.max(1, gross, requested, fulfilled, abnormal);
  el("sim-fulfillment-funnel").innerHTML = rows.map((row, index) => `
    <div class="funnel-step">
      <div>
        <strong>${row.label}</strong>
        <span>${index === 3 ? "取消 / 缺货 / 延迟待发" : "实时累计"}</span>
      </div>
      <div class="funnel-bar"><i style="width:${Math.max(4, row.value / max * 100)}%; background:${row.color}"></i></div>
      <b>${fmt.format(row.value)}</b>
    </div>
  `).join("");
}

function renderPlatformMix(pulse) {
  const mix = pulse.platform_mix || {};
  const entries = Object.entries(mix);
  if (!entries.length) {
    el("sim-platform-mix").innerHTML = `<div class="event-item">启动仿真后展示平台订单占比。</div>`;
    return;
  }
  const colors = ["#1e1f23", "#3f6b4a", "#f54e00", "#2f6b3a"];
  const radius = 42;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  const rings = entries.map(([label, value], index) => {
    const length = Number(value || 0) / 100 * circumference;
    const ring = `<circle r="${radius}" cx="58" cy="58" fill="none" stroke="${colors[index % colors.length]}" stroke-width="16" stroke-dasharray="${length} ${circumference - length}" stroke-dashoffset="${-offset}" transform="rotate(-90 58 58)"></circle>`;
    offset += length;
    return ring;
  }).join("");
  const legend = entries.map(([label, value], index) => `
    <div class="platform-row">
      <i style="background:${colors[index % colors.length]}"></i>
      <span>${label}</span>
      <b>${value}%</b>
    </div>
  `).join("");
  el("sim-platform-mix").innerHTML = `
    <svg viewBox="0 0 116 116" class="donut-chart" role="img" aria-label="平台订单占比">
      <circle r="${radius}" cx="58" cy="58" fill="none" stroke="#e8eef7" stroke-width="16"></circle>
      ${rings}
      <text x="58" y="55" text-anchor="middle">平台</text>
      <text x="58" y="72" text-anchor="middle">${fmt.format(Math.round(pulse.traffic_index || 100))}</text>
    </svg>
    <div class="platform-legend">${legend}</div>
  `;
}

function renderCapacityHeatmap(rows, pulse) {
  const capacityLoad = pulse.capacity_load || {};
  const sorted = [...rows].sort((a, b) => Number(b.capacity_load || capacityLoad[b.warehouse_id] || 0) - Number(a.capacity_load || capacityLoad[a.warehouse_id] || 0));
  el("sim-capacity-heatmap").innerHTML = sorted.length ? sorted.map((row) => {
    const load = Number(row.capacity_load ?? capacityLoad[row.warehouse_id] ?? 0);
    const tone = load >= .85 ? "hot" : load >= .55 ? "warm" : "cool";
    return `
      <div class="heatmap-cell ${tone}">
        <strong>${warehouseName(row.warehouse_id)}</strong>
        <span>负载 ${Math.round(load * 100)}% / 待发 ${fmt.format(row.backlog_units || 0)} 件</span>
        <div class="heat-track"><i style="width:${Math.max(4, Math.min(100, load * 100))}%"></i></div>
        <em>可售 ${row.days_of_supply || "-"} 天 · 近期履约 ${Math.round(Number(row.fill_rate || 0) * 100)}%</em>
      </div>
    `;
  }).join("") : `<div class="event-item">启动仿真后展示仓库产能热力。</div>`;
}

function renderSimulationWarehouses(rows) {
  el("sim-warehouses").innerHTML = rows.length ? rows.map((row) => {
    const risk = Number(row.risk || 0);
    const color = risk >= 0.65 ? "var(--hot)" : risk >= 0.35 ? "var(--amber)" : "var(--green)";
    return `
      <div class="sim-warehouse-card ${riskClassFromValue(risk)}">
        <strong>${warehouseName(row.warehouse_id)}</strong>
        <span>${risk >= 0.65 ? "高风险" : risk >= 0.35 ? "需观察" : "稳定"} / 可售 ${row.days_of_supply} 天</span>
        <div class="sim-card-row">
          <div><b>${fmt.format(row.current_inventory)}</b><em>当前库存</em></div>
          <div><b>${fmt.format(row.inbound_units || 0)}</b><em>在途</em></div>
          <div><b>${row.hourly_demand}</b><em>小时需求</em></div>
          <div><b>${Math.round((row.fill_rate || 0) * 100)}%</b><em>近期履约</em></div>
          <div><b>${Math.round(Number(row.capacity_load || 0) * 100)}%</b><em>产能负载</em></div>
          <div><b>${fmt.format(row.backlog_units || 0)}</b><em>待发</em></div>
        </div>
        <div class="sim-risk-track"><i style="width:${Math.max(4, risk * 100)}%; background:${color}"></i></div>
      </div>
    `;
  }).join("") : `<div class="event-item">启动仿真后显示仓库实时状态。</div>`;
}

function renderSimulationOrders(orders) {
  el("sim-orders").innerHTML = orders.length ? orders.slice(0, 14).map((order) => {
    const badges = [
      order.stockout_units ? `<b class="badge bad">缺货 ${fmt.format(order.stockout_units)}</b>` : "",
      order.delayed_units ? `<b class="badge warn">待发 ${fmt.format(order.delayed_units)}</b>` : "",
      !order.stockout_units && !order.delayed_units ? `<b class="badge ok">履约 ${fmt.format(order.fulfilled_units)}</b>` : "",
    ].filter(Boolean).join("");
    return `
    <div class="sim-feed-item ${order.stockout_units > 0 ? "stockout" : order.delayed_units > 0 ? "delayed" : ""}">
      <div class="feed-head">
        <strong>${order.simulated_time} · ${warehouseName(order.warehouse_id)}</strong>
        <span class="feed-meta">${order.platform || "Amazon"}</span>
      </div>
      <span class="feed-main">${skuName(order.sku)} · 下单 ${fmt.format(order.gross_units || order.requested_units)} · 剩余库存 ${fmt.format(order.inventory_after)}</span>
      <div class="feed-foot">${badges}<span>${order.promotion || order.scenario || "常规"} · 流量 ${order.traffic_index || 100}</span></div>
    </div>
  `;
  }).join("") : `<div class="event-item empty-state">点「开始仿真」后，订单冲击会出现在这里。</div>`;
}

function renderSimulationForecast(rows) {
  el("sim-forecast").innerHTML = rows.length ? rows.map((row) => `
    <div class="sim-rec-item forecast">
      <div>
        <strong>${skuName(row.sku)} -> ${warehouseName(row.warehouse_id)}</strong>
        <span>预测日销 ${row.projected_daily_rate} 件，当前库存 ${fmt.format(row.current_inventory)} 件</span>
        <span>7 天 ${fmt.format(row.forecast_7d)} / 14 天 ${fmt.format(row.forecast_14d)} / 30 天 ${fmt.format(row.forecast_30d)}</span>
      </div>
      <b>${riskLabel(row.risk)}</b>
    </div>
  `).join("") : `<div class="event-item">滚动预测将在模拟日切换后刷新。</div>`;
}

function renderSimulationShipments(simulation) {
  const active = simulation.in_transit_shipments || [];
  const arrivals = simulation.arrivals || [];
  const activeHtml = active.map((item) => {
    const lead = Math.max(1, Number(item.lead_time_hours || 1));
    const remaining = Math.max(0, Number(item.remaining_hours || 0));
    const progress = Math.round((lead - remaining) / lead * 100);
    return `
      <div class="sim-feed-item shipment">
        <strong>${skuName(item.sku)} / ${transportModeName(item.transport_mode)}</strong>
        <span>${item.source_label || warehouseName(item.source_warehouse_id)} -> ${item.destination_label || warehouseName(item.destination_warehouse_id)}，${fmt.format(item.quantity)} 件</span>
        <div class="shipment-bar"><i style="width:${Math.max(6, progress)}%"></i></div>
        <span>剩余 ${remaining} 个模拟小时 / 真实提前期约 ${item.real_lead_time_days || "-"} 天 / ETA 置信 ${Math.round(Number(item.eta_confidence || 0.86) * 100)}%</span>
        <span>${item.delay_reason || "常规在途"}${item.delay_hours ? `，延迟 ${item.delay_hours} 小时` : ""}</span>
      </div>
    `;
  }).join("");
  const arrivalHtml = arrivals.slice(0, 4).map((item) => `
    <div class="sim-feed-item arrived">
      <strong>${item.arrived_time} / 已到仓</strong>
      <span>${skuName(item.sku)} 入库 ${fmt.format(item.quantity)} 件，目的仓 ${item.destination_label || warehouseName(item.destination_warehouse_id)}</span>
    </div>
  `).join("");
  el("sim-shipments").innerHTML = activeHtml + arrivalHtml || `<div class="event-item">滚动决策后会生成在途补货或跨仓调拨。</div>`;
}

function renderSimulationRecommendations(rows) {
  el("sim-recommendations").innerHTML = rows.length ? rows.map((row) => `
    <div class="sim-rec-item">
      <div>
        <strong>${skuName(row.sku)} -> ${warehouseName(row.warehouse_id)}</strong>
        <span>当前库存 ${fmt.format(row.current_inventory)} 件，在途 ${fmt.format(row.inbound_units || 0)} 件，预测日销 ${row.projected_daily_rate} 件</span>
        <span>${row.reason}</span>
      </div>
      <b>${fmt.format(row.suggested_units)} 件</b>
    </div>
  `).join("") : `<div class="event-item">当前库存仍在目标水位以上，暂无动态补货建议。</div>`;
}

function renderSimulationEvents(simulation) {
  const incidents = (simulation.active_incidents || []).map((item) => `
    <div class="sim-feed-item">
      <strong>${item.label}</strong>
      <span>${item.impact}</span>
    </div>
  `).join("");
  const events = (simulation.events || []).slice(0, 12).map((event) => `
    <div class="sim-feed-item">
      <strong>${event.type} / Tick ${event.tick}</strong>
      <span>${event.message}</span>
    </div>
  `).join("");
  el("sim-events").innerHTML = incidents + events || `<div class="event-item">暂无仿真事件。</div>`;
}

function simulationTime(minutes, historyDays = 180) {
  const day = Number(historyDays || 180) + Math.floor(minutes / 1440) + 1;
  const hour = Math.floor((minutes % 1440) / 60);
  return `D${day} ${String(hour).padStart(2, "0")}:00`;
}

function transportModeName(mode) {
  if (mode === "AIR") return "空运";
  if (mode === "SEA") return "海运";
  if (mode === "TRANSFER") return "跨仓调拨";
  return mode || "待定";
}

function transportModeLabel(value) {
  return String(value || "待定")
    .split("+")
    .filter(Boolean)
    .map((mode) => transportModeName(mode))
    .join(" + ");
}

function riskLabel(value) {
  const risk = Number(value || 0);
  if (risk >= 0.65) return "高风险";
  if (risk >= 0.35) return "需观察";
  return "稳定";
}

function setSimulationTimer(jobId, running, scenario) {
  if (simulationTimer) {
    clearInterval(simulationTimer);
    simulationTimer = null;
  }
  if (!running) return;
  simulationTimer = setInterval(() => tickSimulation(jobId, scenario), 1800);
}

async function startSimulation(jobId, scenario) {
  currentRun = await requestJson(`/student/runs/${jobId}/simulation/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario, steps: 1 }),
  });
  renderRun(currentRun, { mode: "live" });
}

async function stopSimulation(jobId) {
  currentRun = await requestJson(`/student/runs/${jobId}/simulation/stop`, { method: "POST" });
  renderRun(currentRun, { mode: "live" });
}

async function resetSimulation(jobId) {
  currentRun = await requestJson(`/student/runs/${jobId}/simulation/reset`, { method: "POST" });
  renderRun(currentRun, { mode: "full" });
}

async function tickSimulation(jobId, scenario) {
  if (!currentRun || currentRun.metadata.job_id !== jobId) return;
  try {
    currentRun = await requestJson(`/student/runs/${jobId}/simulation/tick`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario, steps: 1 }),
    });
    renderRun(currentRun, { mode: "live" });
  } catch (error) {
    showMessage(error.message);
    setSimulationTimer(jobId, false, scenario);
  }
}

function renderForecast(plan) {
  const recommendations = plan.recommendations || [];
  renderModelSummary(plan, recommendations);
  const skuOptions = [...new Set(recommendations.map((row) => row.sku))].sort();
  const selector = el("forecast-sku");
  const countrySelector = el("forecast-country");
  const warehouseSelector = el("forecast-warehouse");
  const periodSelector = el("forecast-period");
  const previous = selector.value;
  selector.innerHTML = skuOptions.map((sku) => `<option value="${sku}">${skuName(sku)}</option>`).join("");
  selector.value = skuOptions.includes(previous) ? previous : skuOptions[0] || "";
  const countries = [...new Set(recommendations.map((row) => countryFromWarehouse(row.warehouse_id)))].sort();
  const previousCountry = countrySelector.value;
  countrySelector.innerHTML = countries.map((country) => `<option value="${country}">${COUNTRY_NAMES[country] || country}</option>`).join("");
  countrySelector.value = countries.includes(previousCountry) ? previousCountry : countries[0] || "";

  const syncWarehouseOptions = () => {
    const rows = recommendations.filter((row) => row.sku === selector.value && countryFromWarehouse(row.warehouse_id) === countrySelector.value);
    const warehouses = [...new Set(rows.map((row) => row.warehouse_id))].sort();
    const previousWarehouse = warehouseSelector.value;
    warehouseSelector.innerHTML = warehouses.map((id) => `<option value="${id}">${warehouseName(id)}</option>`).join("");
    warehouseSelector.value = warehouses.includes(previousWarehouse) ? previousWarehouse : warehouses[0] || "";
  };
  const renderSelected = () => {
    syncWarehouseOptions();
    renderForecastSku(plan, selector.value, warehouseSelector.value, Number(periodSelector.value || 30));
  };
  selector.onchange = renderSelected;
  countrySelector.onchange = renderSelected;
  warehouseSelector.onchange = () => renderForecastSku(plan, selector.value, warehouseSelector.value, Number(periodSelector.value || 30));
  periodSelector.onchange = () => renderForecastSku(plan, selector.value, warehouseSelector.value, Number(periodSelector.value || 30));
  renderSelected();

  el("forecast-table").innerHTML = recommendations
    .slice()
    .sort((a, b) => b.p80_units - a.p80_units)
    .map((row) => `
      <tr>
        <td>${skuName(row.sku)}</td>
        <td>${row.model_name}</td>
        <td>${warehouseName(row.warehouse_id)}</td>
        <td class="num">${row.daily_rate}</td>
        <td class="num">${fmt.format(row.p80_units)}</td>
        <td class="num">${fmt.format(row.p95_units)}</td>
        <td class="num">${row.urgency}</td>
        <td>${row.reason}</td>
      </tr>
    `).join("");
}

function renderForecastSku(plan, sku, warehouseId, period = 30) {
  const rows = (plan.recommendations || []).filter((row) => row.sku === sku);
  const selected = rows.find((row) => row.warehouse_id === warehouseId) || rows[0];
  if (selected) {
    renderForecastDecisionBoard(selected, period);
    renderForecastFactors(selected, period);
  }
  renderForecastVisual(rows, period);
  el("forecast-chart").innerHTML = rows.map((row) => `
    <div class="forecast-signal-card ${Number(row.urgency || 0) >= 0.65 ? "hot" : Number(row.urgency || 0) >= 0.35 ? "warm" : "calm"}">
      <span>${warehouseName(row.warehouse_id)}</span>
      <strong>${fmt.format(scaleForecast(row.p80_units, period))} 件</strong>
      <p>${period} 天 P80 需求 · 可售 ${fmt.format(Math.round(Number(row.inventory_position || 0) / Math.max(1, Number(row.daily_rate || 0))))} 天</p>
    </div>
  `).join("");
}

function renderForecastDecisionBoard(row, period) {
  const dailyRate = Number(row.daily_rate || 0);
  const demand7 = Math.round(dailyRate * 7);
  const demand14 = Math.round(dailyRate * 14);
  const demand30 = scaleForecast(row.p80_units, 30);
  const periodDemand = scaleForecast(row.p80_units, period);
  const currentInventory = Number(row.inventory_position || 0);
  const safetyStock = Math.max(0, Number(row.target_stock || 0) - Number(row.p80_units || 0));
  const expectedCoverageDays = Math.round(currentInventory / Math.max(1, dailyRate));
  const gap = Math.max(0, periodDemand + safetyStock - currentInventory);
  const urgency = Number(row.urgency || 0);
  const riskLabelText = urgency >= 0.65 ? "高风险" : urgency >= 0.35 ? "需观察" : "稳定";
  const maxDemand = Math.max(1, demand7, demand14, demand30);
  const horizonCard = (label, value, accent) => `
    <div class="forecast-horizon-card ${accent}">
      <span>${label}</span>
      <strong>${fmt.format(value)} 件</strong>
      <em>预测需求</em>
    </div>
  `;
  el("forecast-curve").innerHTML = `
    <section class="forecast-decision-board">
      <div class="forecast-conclusion">
        <div>
          <span class="section-title">预测结论</span>
          <h3>${skuName(row.sku)} / ${warehouseName(row.warehouse_id)}</h3>
          <p>未来 ${period} 天预计需求 ${fmt.format(periodDemand)} 件，当前库存约可支撑 ${fmt.format(expectedCoverageDays)} 天。</p>
        </div>
        <strong class="${urgency >= 0.65 ? "risk-high" : urgency >= 0.35 ? "risk-mid" : "risk-low"}">${riskLabelText}</strong>
      </div>
      <div class="forecast-horizon-grid">
        ${horizonCard("未来 7 天", demand7, "near")}
        ${horizonCard("未来 14 天", demand14, "mid")}
        ${horizonCard("未来 30 天", demand30, "long")}
      </div>
      <div class="forecast-decision-grid">
        <div class="forecast-demand-bars">
          <div class="decision-row"><span>7 天</span><div><i style="width:${demand7 / maxDemand * 100}%"></i></div><b>${fmt.format(demand7)}</b></div>
          <div class="decision-row"><span>14 天</span><div><i style="width:${demand14 / maxDemand * 100}%"></i></div><b>${fmt.format(demand14)}</b></div>
          <div class="decision-row"><span>30 天</span><div><i style="width:${demand30 / maxDemand * 100}%"></i></div><b>${fmt.format(demand30)}</b></div>
        </div>
        <div class="inventory-coverage-box">
          <div><span>当前库存</span><strong>${fmt.format(currentInventory)} 件</strong></div>
          <div><span>安全库存</span><strong>${fmt.format(safetyStock)} 件</strong></div>
          <div><span>${period} 天缺口</span><strong>${fmt.format(gap)} 件</strong></div>
        </div>
      </div>
    </section>
  `;
}

function renderForecastFactors(row, period) {
  const demand = scaleForecast(row.p80_units, period);
  const growth = Math.round(18 + Number(row.urgency || 0) * 32);
  const factors = [
    { factor: row.urgency >= 0.55 ? "库存低于安全水位" : "库存水位可观察", impact: row.urgency >= 0.55 ? "缺货风险上升" : "维持基础安全库存", tone: row.urgency >= 0.55 ? "risk" : "stable" },
    { factor: "近 7 天销量滚动增长", impact: "需求上升", tone: "up" },
    { factor: row.sku.startsWith("HUB") ? "高毛利 3C 配件" : "高频 3C 配件", impact: row.sku.startsWith("HUB") ? "优先保障供给" : "补货频率较高", tone: "up" },
    { factor: "周末与平台活动波动", impact: "短期需求上升", tone: "up" },
  ];
  el("forecast-factors").innerHTML = factors.map((item) => `
    <div class="factor-item ${item.tone}">
      <strong>${item.factor}</strong>
      <span>${item.impact}</span>
    </div>
  `).join("");
  el("forecast-explain").textContent = `系统预测未来 ${period} 天 ${warehouseName(row.warehouse_id)} 的 ${skuName(row.sku)} 需求约为 ${fmt.format(demand)} 件，较过去同周期增长约 ${growth}%。主要原因是近期销量抬升、促销和周末效应叠加，同时当前库存与安全库存距离收窄。`;
}

function scaleForecast(value, period) {
  return Math.round(Number(value || 0) * Number(period || 30) / 30);
}

function countryFromWarehouse(warehouseId) {
  if (warehouseId.startsWith("US")) return "US";
  if (warehouseId.startsWith("DE")) return "DE";
  if (warehouseId.startsWith("UK")) return "UK";
  if (warehouseId.startsWith("AU")) return "AU";
  return warehouseId;
}

function renderReplenishment(jobId, plan, state) {
  renderUrgencyMatrix(plan);
  const simulation = state.simulation || {};
  const inventory = simulation.inventory || {};
  const inbound = {};
  (simulation.in_transit_shipments || []).forEach((item) => {
    const key = `${item.sku}||${item.destination_warehouse_id}`;
    inbound[key] = (inbound[key] || 0) + Number(item.quantity || 0);
  });
  const allocationByKey = Object.fromEntries((plan.allocations || []).map((row) => [`${row.sku}||${row.warehouse_id}`, row]));
  const recRows = [...(plan.recommendations || [])]
    .sort((a, b) => b.recommended_units - a.recommended_units)
    .slice(0, 10)
    .map((row) => ({
      label: `${skuName(row.sku)} / ${warehouseName(row.warehouse_id)}`,
      value: row.recommended_units,
      color: row.urgency >= 0.65 ? "var(--hot)" : "var(--charcoal)",
    }));
  el("replenishment-chart").innerHTML = barChart(recRows);

  el("replenishments").innerHTML = (plan.recommendations || [])
    .map((row) => `
      ${renderReplenishmentRow(row, inventory, inbound, allocationByKey)}
    `).join("");
  renderApprovals(jobId, state.approvals || []);
}

function renderReplenishmentRow(row, inventory, inbound, allocationByKey) {
  const key = `${row.sku}||${row.warehouse_id}`;
  const currentInventory = Number(inventory[key] ?? row.inventory_position ?? 0);
  const inboundUnits = Number(inbound[key] || 0);
  const forecast30 = Number(row.p80_units || 0);
  const safetyStock = Math.max(0, Number(row.target_stock || 0) - forecast30);
  const suggested = Math.max(0, Math.round(forecast30 + safetyStock - currentInventory - inboundUnits));
  const allocation = allocationByKey[key] || {};
  const priority = Number(row.urgency || 0) >= 0.65 ? "高" : Number(row.urgency || 0) >= 0.35 ? "中" : "低";
  const formula = `${fmt.format(forecast30)} + ${fmt.format(safetyStock)} - ${fmt.format(currentInventory)} - ${fmt.format(inboundUnits)} = ${fmt.format(suggested)}`;
  return `
    <tr>
      <td>${skuName(row.sku)}</td>
      <td>${warehouseName(row.warehouse_id)}</td>
      <td class="num">${fmt.format(currentInventory)}</td>
      <td class="num">${fmt.format(inboundUnits)}</td>
      <td class="num">${fmt.format(forecast30)}</td>
      <td class="num">${fmt.format(safetyStock)}</td>
      <td class="num"><strong>${fmt.format(suggested)}</strong></td>
      <td>${transportModeLabel(allocation.transport_mode || (priority === "高" ? "AIR" : "SEA"))}</td>
      <td><span class="priority ${priority === "高" ? "high" : priority === "中" ? "mid" : "low"}">${priority}</span></td>
      <td><code>${formula}</code></td>
    </tr>
  `;
}

function renderApprovals(jobId, approvals) {
  const body = el("approvals");
  if (!approvals.length) {
    body.innerHTML = `<tr><td colspan="7">当前没有需要审批的补货单。</td></tr>`;
    return;
  }
  body.innerHTML = approvals.map((row) => `
    <tr>
      <td>${row.approval_id}</td>
      <td>${skuName(row.sku)}</td>
      <td>${warehouseName(row.warehouse_id)}</td>
      <td class="num">${fmt.format(row.recommended_units)}</td>
      <td class="num">${fmt.format(row.allocated_units)}</td>
      <td><span class="status ${statusClass(row.status)}">${row.status}</span></td>
      <td>
        <div class="approval-actions">
          <input data-note="${row.approval_id}" value="${row.note || ""}" placeholder="审批备注">
          <div>
            <button class="approve" data-approval="${row.approval_id}" data-decision="批准">批准</button>
            <button class="reject" data-approval="${row.approval_id}" data-decision="驳回">驳回</button>
          </div>
        </div>
      </td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-approval]").forEach((button) => {
    button.addEventListener("click", async () => {
      const approvalId = button.dataset.approval;
      const note = body.querySelector(`[data-note="${approvalId}"]`).value;
      currentRun = await requestJson(`/student/runs/${jobId}/approvals/${approvalId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: button.dataset.decision, note }),
      });
      renderRun(currentRun);
      });
  });
  renderApprovalFunnel(approvals);
}

function renderAllocation(jobId, plan, state) {
  renderAllocationFlow(plan);
  renderAllocationComparison(plan);
  const modeTotals = {};
  (plan.allocations || []).forEach((row) => {
    const modes = String(row.transport_mode || "").split("+").filter((mode) => mode && mode !== "未分配");
    const share = modes.length ? row.allocated_units / modes.length : 0;
    modes.forEach((mode) => { modeTotals[mode] = (modeTotals[mode] || 0) + share; });
  });
  const transportRows = Object.entries(modeTotals).map(([label, value]) => ({
    label,
    value,
    color: label === "AIR" ? "var(--amber)" : "var(--teal)",
  }));
  el("transport-chart").innerHTML = barChart(transportRows);
  renderExecutionForm(jobId, plan.allocations || [], state.execution_events || []);
  renderAllocations(plan);
}

function renderExecutionForm(jobId, allocations, events) {
  const skus = [...new Set(allocations.map((row) => row.sku))].sort();
  const warehouses = [...new Set(allocations.map((row) => row.warehouse_id))].sort();
  el("exec-sku").innerHTML = skus.map((sku) => `<option value="${sku}">${skuName(sku)}</option>`).join("");
  el("exec-warehouse").innerHTML = warehouses.map((id) => `<option value="${id}">${warehouseName(id)}</option>`).join("");
  el("events").innerHTML = events.length
    ? [...events].reverse().map((event) => `
      <div class="event-item">
        <strong>${skuName(event.sku)} -> ${warehouseName(event.warehouse_id)} / ${fmt.format(event.executed_units)} 件 / ${event.execution_status}</strong>
        <span>${event.created_at} ${event.note || ""}</span>
      </div>
    `).join("")
    : `<div class="event-item">尚未回写执行结果。</div>`;

  el("execution-form").onsubmit = async (event) => {
    event.preventDefault();
    currentRun = await requestJson(`/student/runs/${jobId}/execution`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sku: el("exec-sku").value,
        warehouse_id: el("exec-warehouse").value,
        executed_units: Number(el("exec-units").value),
        execution_status: el("exec-status").value,
        note: el("exec-note").value,
      }),
    });
    el("exec-note").value = "";
    renderRun(currentRun);
  };
}

function allocationFocusRows(plan) {
  const totals = {};
  (plan.allocations || []).forEach((row) => {
    totals[row.sku] = (totals[row.sku] || 0) + Number(row.allocated_units || 0);
  });
  const focusSku = Object.entries(totals).sort((a, b) => b[1] - a[1])[0]?.[0] || (plan.allocations || [])[0]?.sku;
  const recByWarehouse = Object.fromEntries((plan.recommendations || [])
    .filter((row) => row.sku === focusSku)
    .map((row) => [row.warehouse_id, row]));
  return (plan.allocations || [])
    .filter((row) => row.sku === focusSku)
    .map((row) => {
      const rec = recByWarehouse[row.warehouse_id] || {};
      const gap = Math.max(0, Number(rec.p80_units || 0) + Math.max(0, Number(rec.target_stock || 0) - Number(rec.p80_units || 0)) - Number(rec.inventory_position || 0));
      return {
        ...row,
        focusSku,
        forecastDemand: Number(rec.p80_units || 0),
        currentInventory: Number(rec.inventory_position || 0),
        gap,
        reason: allocationReason(row.warehouse_id, gap, Number(rec.urgency || 0)),
      };
    }).sort((a, b) => b.allocated_units - a.allocated_units);
}

function renderAllocationComparison(plan) {
  const rows = allocationFocusRows(plan);
  const total = 1000;
  const average = rows.length ? Math.round(total / rows.length) : 0;
  const smartTotal = rows.reduce((sum, row) => sum + Number(row.allocated_units || 0), 0) || 1;
  const smartValues = rows.map((row) => Math.round(Number(row.allocated_units || 0) / smartTotal * total));
  const maxUnits = Math.max(1, average, ...smartValues);
  el("allocation-comparison").innerHTML = `
    <div class="compare-head"><strong>${skuName(rows[0]?.focusSku || "HUB-7IN1")}</strong><span>传统平均分配会忽略各仓风险，智能分仓按需求、缺口、仓容和运输成本分配。</span></div>
    <div class="compare-grid">
      ${rows.map((row, index) => {
        const smart = smartValues[index];
        return `
          <div class="compare-row">
            <strong>${warehouseName(row.warehouse_id)}</strong>
            <div class="compare-bars">
              <div class="compare-line">
                <span>平均</span>
                <div><i class="baseline" style="width:${Math.max(6, average / maxUnits * 100)}%"></i></div>
                <b>${fmt.format(average)} 件</b>
              </div>
              <div class="compare-line">
                <span>智能</span>
                <div><i class="smart" style="width:${Math.max(6, smart / maxUnits * 100)}%"></i></div>
                <b>${fmt.format(smart)} 件</b>
              </div>
            </div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderAllocations(plan) {
  el("allocations").innerHTML = allocationFocusRows(plan).map((row) => `
    <tr>
      <td>${warehouseName(row.warehouse_id)}</td>
      <td class="num">${fmt.format(row.forecastDemand)}</td>
      <td class="num">${fmt.format(row.currentInventory)}</td>
      <td class="num">${fmt.format(row.gap)}</td>
      <td class="num">${fmt.format(row.allocated_units)}</td>
      <td>${row.reason}</td>
      <td>${transportModeLabel(row.transport_mode)}</td>
      <td>${row.optimizer}</td>
    </tr>
  `).join("");
}

function allocationReason(warehouseId, gap, urgency) {
  if (warehouseId === "US-LAX" && urgency >= 0.45) return "美国需求高，缺货风险高";
  if (warehouseId === "UK-BHX" && gap > 0) return "当前库存偏低，可由欧洲库存池协同";
  if (warehouseId === "DE-FRA") return "欧洲需求稳定，维持区域水位";
  if (warehouseId === "AU-SYD") return "远距离市场，维持基础安全库存";
  return gap > 0 ? "预测缺口需要补足" : "保持安全库存水位";
}

function renderEvaluation(plan) {
  const sim = plan.simulation_comparison;
  renderEvaluationScoreboard(plan);
  el("evaluation-metrics").innerHTML = [
    metric("Fill Rate 提升", `+${sim.improvement.fill_rate_lift_pct}pct`),
    metric("缺货率下降", `-${sim.improvement.stockout_rate_reduction_pct}pct`),
    metric("配送缩短", `-${sim.improvement.delivery_days_reduction}天`),
    metric("利润提升", fmt.format(Math.round(sim.improvement.profit_lift || 0))),
  ].join("");
  const rows = [
    { label: "智能满足率", value: percentValue(sim.intelligent.fill_rate), color: "var(--green)" },
    { label: "传统满足率", value: percentValue(sim.baseline.fill_rate), color: "#9aa7b7" },
    { label: "智能缺货率", value: percentValue(sim.intelligent.stockout_rate), color: "var(--green)" },
    { label: "传统缺货率", value: percentValue(sim.baseline.stockout_rate), color: "#9aa7b7" },
    { label: "智能配送时效", value: sim.intelligent.average_delivery_days, color: "var(--green)" },
    { label: "传统配送时效", value: sim.baseline.average_delivery_days, color: "#9aa7b7" },
    { label: "智能总利润", value: sim.intelligent.total_profit, color: "var(--green)" },
    { label: "传统总利润", value: sim.baseline.total_profit, color: "#9aa7b7" },
  ];
  el("evaluation-chart").innerHTML = barChart(rows, (value) => value <= 100 ? value.toFixed(1) : fmt.format(Math.round(value)));
}

function percentValue(value) {
  return Math.round(Number(value || 0) * 1000) / 10;
}

function categoryFromSku(sku) {
  if (sku.startsWith("CAB")) return "USB-C 快充线";
  if (sku.startsWith("STAND")) return "手机支架";
  if (sku.startsWith("APCASE")) return "耳机保护壳";
  if (sku.startsWith("IPAD")) return "平板保护壳";
  if (sku.startsWith("HUB")) return "USB-C 拓展坞";
  if (sku.startsWith("LAPSTAND")) return "笔记本支架";
  return "其他 3C 配件";
}

function activatePage(pageId) {
  document.querySelectorAll(".page-tab").forEach((item) => item.classList.toggle("active", item.dataset.page === pageId));
  document.querySelectorAll(".page").forEach((item) => {
    const on = item.id === pageId;
    item.classList.toggle("active", on);
    if (on) {
      item.style.animation = "none";
      // reflow to restart enter motion for panels inside
      void item.offsetWidth;
      item.style.animation = "";
    }
  });
}

document.querySelectorAll(".page-tab").forEach((button) => {
  button.addEventListener("click", () => activatePage(button.dataset.page));
});

el("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showLoginMessage("");
  try {
    await requestJson("/student/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: el("password").value }),
    });
    el("password").value = "";
    setAuthenticated(true);
    if (document.body.classList.contains("simulation-only")) {
      await loadLiveDemo();
    } else {
      await loadRuns();
    }
  } catch (error) {
    showLoginMessage(error.message);
  }
});

el("logout-button").addEventListener("click", async () => {
  await requestJson("/student/logout", { method: "POST" }).catch(() => ({}));
  setAuthenticated(false);
  showLoginMessage("");
});

el("new-run-button").addEventListener("click", async () => {
  showMessage("");
  const button = el("new-run-button");
  button.disabled = true;
  button.textContent = "仿真生成中";
  try {
    currentRun = await requestJson("/student/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ horizon_days: 30 }),
    });
    renderRun(currentRun);
    await loadRuns();
    showMessage("仿真实验已生成。", false);
  } catch (error) {
    showMessage(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "新建仿真实验";
  }
});

async function initialize() {
  try {
    const session = await requestJson("/student/session");
    setAuthenticated(session.authenticated);
    if (authenticated) {
      if (document.body.classList.contains("simulation-only")) {
        await loadLiveDemo();
      } else {
        await loadRuns();
      }
    }
  } catch (error) {
    setAuthenticated(false);
    showLoginMessage(error.message);
  }
}

initialize();
