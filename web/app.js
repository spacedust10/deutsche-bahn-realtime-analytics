/* Realtime dashboard client.
 *
 * One WebSocket carries the whole dashboard payload; every chart is created
 * once and then fed with setOption, which is what lets ECharts tween between
 * states instead of redrawing. Redrawing on each push would flicker and throw
 * away the animation that makes a realtime view readable. */

const PALETTE = {
  red: '#ec0016', redLo: '#ff4b57', amber: '#ffb020',
  green: '#22c98a', cyan: '#35c8e8', violet: '#8b7cf6',
  text: '#e8eef7', dim: '#8296ae', mute: '#56677d',
  line: '#1e2836', panel: '#0e131c',
};

const PUNCTUAL_SECONDS = 360;  // DB's own definition of "on time".

const charts = {};
let selectedTrip = null;
let stationGeo = [];   // Static station coordinates, fetched once.

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const minutes = (seconds) => (seconds || 0) / 60;

function delayColour(seconds) {
  if (seconds < PUNCTUAL_SECONDS) return PALETTE.green;
  if (seconds < 900) return PALETTE.amber;
  return PALETTE.red;
}

function delayClass(seconds) {
  if (seconds < PUNCTUAL_SECONDS) return 'd-ok';
  if (seconds < 900) return 'd-warn';
  return 'd-bad';
}

function clockTime(iso) {
  if (!iso) return '--:--:--';
  return new Date(iso).toLocaleTimeString('de-DE', { hour12: false, timeZone: 'Europe/Berlin' });
}

/* Count from the previous value to the new one so numbers feel live rather
 * than snapping. Each element keeps its own animation frame handle. */
function animateCounter(el, target, decimals = 0) {
  const from = parseFloat(el.dataset.current || '0');
  const to = Number.isFinite(target) ? target : 0;
  if (from === to) return;

  el.dataset.current = String(to);
  cancelAnimationFrame(Number(el.dataset.raf || 0));

  const started = performance.now();
  const DURATION = 850;

  const step = (now) => {
    const t = Math.min((now - started) / DURATION, 1);
    const eased = 1 - Math.pow(1 - t, 3);                 // easeOutCubic
    const value = from + (to - from) * eased;
    el.textContent = decimals
      ? value.toFixed(decimals)
      : Math.round(value).toLocaleString('en-US');
    if (t < 1) el.dataset.raf = String(requestAnimationFrame(step));
  };
  el.dataset.raf = String(requestAnimationFrame(step));
}

function setCounter(key, value, decimals = 0) {
  const el = document.querySelector(`[data-counter="${key}"]`);
  if (el) animateCounter(el, value, decimals);
}

const BASE_OPTION = {
  animationDuration: 750,
  animationDurationUpdate: 800,
  animationEasing: 'cubicOut',
  animationEasingUpdate: 'cubicInOut',
  textStyle: { fontFamily: 'Inter, -apple-system, sans-serif', color: PALETTE.dim },
  tooltip: {
    backgroundColor: 'rgba(10,14,20,.95)',
    borderColor: PALETTE.line,
    textStyle: { color: PALETTE.text, fontSize: 12 },
    padding: [8, 12],
  },
};

const AXIS = {
  axisLine: { lineStyle: { color: PALETTE.line } },
  axisTick: { show: false },
  axisLabel: { color: PALETTE.mute, fontSize: 10.5 },
  splitLine: { lineStyle: { color: 'rgba(30,40,54,.55)', type: 'dashed' } },
};

function chart(id) {
  if (!charts[id]) {
    charts[id] = echarts.init(document.getElementById(id), null, { renderer: 'canvas' });
  }
  return charts[id];
}

// ---------------------------------------------------------------------------
// charts
// ---------------------------------------------------------------------------

function renderTimeseries(rows) {
  const times = rows.map((r) => clockTime(r.bucket));
  chart('chart-timeseries').setOption({
    ...BASE_OPTION,
    grid: { left: 46, right: 52, top: 34, bottom: 26 },
    legend: {
      data: ['Mean delay', 'P90 delay', 'Punctuality'],
      textStyle: { color: PALETTE.dim, fontSize: 11 },
      itemWidth: 14, itemHeight: 8, top: 0, right: 0,
    },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(236,0,22,.45)' } } },
    xAxis: { type: 'category', data: times, boundaryGap: false, ...AXIS, splitLine: { show: false } },
    yAxis: [
      { type: 'value', name: 'min', nameTextStyle: { color: PALETTE.mute, fontSize: 10 }, ...AXIS },
      { type: 'value', name: '%', min: 0, max: 100, position: 'right',
        nameTextStyle: { color: PALETTE.mute, fontSize: 10 }, ...AXIS, splitLine: { show: false } },
    ],
    series: [
      {
        name: 'Mean delay', type: 'line', smooth: 0.35, symbol: 'none',
        data: rows.map((r) => +minutes(r.mean_delay_seconds).toFixed(2)),
        lineStyle: { width: 2.4, color: PALETTE.red },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(236,0,22,.42)' },
            { offset: 1, color: 'rgba(236,0,22,.02)' },
          ]),
        },
      },
      {
        name: 'P90 delay', type: 'line', smooth: 0.35, symbol: 'none',
        data: rows.map((r) => +minutes(r.p90_delay_seconds).toFixed(2)),
        lineStyle: { width: 1.6, color: PALETTE.amber, type: 'dashed' },
      },
      {
        name: 'Punctuality', type: 'line', smooth: 0.35, yAxisIndex: 1,
        symbol: 'circle', symbolSize: 5, showSymbol: false,
        data: rows.map((r) => r.punctuality_pct),
        lineStyle: { width: 2, color: PALETTE.green },
        itemStyle: { color: PALETTE.green },
      },
    ],
  });
}

function renderMap(trains) {
  const located = trains.filter((t) => t.stop_lat && t.stop_lon);
  const late = located.filter((t) => t.current_delay_seconds >= 900);
  const rest = located.filter((t) => t.current_delay_seconds < 900);

  const point = (t) => ({
    value: [t.stop_lon, t.stop_lat, t.current_delay_seconds],
    name: `${t.route_name || t.route_category} · ${t.stop_name}`,
    itemStyle: { color: delayColour(t.current_delay_seconds) },
  });

  chart('chart-map').setOption({
    ...BASE_OPTION,
    grid: { left: 6, right: 6, top: 8, bottom: 6 },
    tooltip: {
      ...BASE_OPTION.tooltip,
      formatter: (p) => `${p.name}<br/><b>${(p.value[2] / 60).toFixed(1)} min</b> delay`,
    },
    // Longitude/latitude on a plain cartesian grid: GTFS-RT TripUpdates carry
    // no coordinates, so trains are drawn at their last reported station.
    xAxis: { type: 'value', min: 5.6, max: 15.4, show: false },
    yAxis: { type: 'value', min: 47.1, max: 55.2, show: false },
    series: [
      {
        // Every long-distance station, dimmed: without it 130 trains read as
        // scattered dots rather than as a rail network over Germany.
        type: 'scatter', silent: true, symbolSize: 2.2, animation: false,
        data: stationGeo.map((s) => [s.stop_lon, s.stop_lat]),
        itemStyle: { color: '#2b3a4d', opacity: 0.75 },
      },
      {
        type: 'scatter', data: rest.map(point),
        symbolSize: (v) => 7 + Math.min(v[2] / 200, 9),
        itemStyle: { opacity: 0.92, borderColor: 'rgba(0,0,0,.5)', borderWidth: 0.5 },
      },
      {
        // Ripple draws the eye to the trains that are actually in trouble.
        type: 'effectScatter', data: late.map(point),
        symbolSize: (v) => 9 + Math.min(v[2] / 200, 12),
        rippleEffect: { brushType: 'stroke', scale: 3, period: 3.4 },
        zlevel: 1,
      },
    ],
  });
}

function renderDistribution(rows) {
  const colours = {
    'early': PALETTE.cyan, 'on time (<6 min)': PALETTE.green, '6-15 min': PALETTE.amber,
    '15-30 min': '#ff8c42', '30-60 min': PALETTE.redLo, '60+ min': PALETTE.red,
  };
  chart('chart-distribution').setOption({
    ...BASE_OPTION,
    grid: { left: 44, right: 16, top: 18, bottom: 46 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'item' },
    xAxis: { type: 'category', data: rows.map((r) => r.band), ...AXIS,
      splitLine: { show: false }, axisLabel: { ...AXIS.axisLabel, interval: 0, rotate: 26 } },
    yAxis: { type: 'value', ...AXIS },
    series: [{
      type: 'bar', data: rows.map((r) => ({
        value: r.stops,
        itemStyle: { color: colours[r.band] || PALETTE.dim, borderRadius: [4, 4, 0, 0] },
      })),
      barMaxWidth: 46,
    }],
  });
}

function renderStations(rows) {
  const ordered = [...rows].reverse();   // ECharts y-axis builds bottom-up.
  chart('chart-stations').setOption({
    ...BASE_OPTION,
    grid: { left: 132, right: 40, top: 10, bottom: 24 },
    tooltip: {
      ...BASE_OPTION.tooltip, trigger: 'item',
      formatter: (p) => `${p.name}<br/><b>${p.value.toFixed(1)} min</b> mean · ${ordered[p.dataIndex].observations} obs`,
    },
    xAxis: { type: 'value', name: 'min', nameTextStyle: { color: PALETTE.mute, fontSize: 10 }, ...AXIS },
    yAxis: { type: 'category', data: ordered.map((r) => r.stop_name), ...AXIS,
      splitLine: { show: false }, axisLabel: { ...AXIS.axisLabel, fontSize: 10.5, width: 122, overflow: 'truncate' } },
    series: [{
      type: 'bar',
      data: ordered.map((r) => ({
        value: +minutes(r.mean_delay_seconds).toFixed(2),
        itemStyle: { color: delayColour(r.mean_delay_seconds), borderRadius: [0, 4, 4, 0] },
      })),
      barMaxWidth: 13,
    }],
  });
}

function renderCategories(rows) {
  chart('chart-categories').setOption({
    ...BASE_OPTION,
    grid: { left: 44, right: 46, top: 30, bottom: 28 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis' },
    legend: { data: ['Mean delay', 'Punctuality'], textStyle: { color: PALETTE.dim, fontSize: 11 },
      itemWidth: 14, itemHeight: 8, top: 0, right: 0 },
    xAxis: { type: 'category', data: rows.map((r) => r.route_category), ...AXIS, splitLine: { show: false } },
    yAxis: [
      { type: 'value', name: 'min', nameTextStyle: { color: PALETTE.mute, fontSize: 10 }, ...AXIS },
      { type: 'value', name: '%', min: 0, max: 100, position: 'right',
        nameTextStyle: { color: PALETTE.mute, fontSize: 10 }, ...AXIS, splitLine: { show: false } },
    ],
    series: [
      {
        name: 'Mean delay', type: 'bar', barMaxWidth: 40,
        data: rows.map((r) => ({
          value: +minutes(r.mean_delay_seconds).toFixed(2),
          itemStyle: { color: delayColour(r.mean_delay_seconds), borderRadius: [4, 4, 0, 0] },
        })),
      },
      {
        name: 'Punctuality', type: 'line', yAxisIndex: 1, smooth: true,
        data: rows.map((r) => r.punctuality_pct),
        lineStyle: { width: 2, color: PALETTE.cyan }, itemStyle: { color: PALETTE.cyan }, symbolSize: 7,
      },
    ],
  });
}

function renderPropagation(rows, label) {
  document.getElementById('prop-hint').textContent = label || 'select a train';
  chart('chart-propagation').setOption({
    ...BASE_OPTION,
    grid: { left: 46, right: 20, top: 20, bottom: 58 },
    tooltip: { ...BASE_OPTION.tooltip, trigger: 'axis' },
    xAxis: { type: 'category', data: rows.map((r) => r.stop_name), ...AXIS, splitLine: { show: false },
      axisLabel: { ...AXIS.axisLabel, rotate: 30, interval: 0, width: 90, overflow: 'truncate' } },
    yAxis: { type: 'value', name: 'min', nameTextStyle: { color: PALETTE.mute, fontSize: 10 }, ...AXIS },
    series: [{
      type: 'line', smooth: 0.3, symbol: 'circle', symbolSize: 8,
      data: rows.map((r) => ({
        value: +minutes(r.arrival_delay ?? r.departure_delay).toFixed(2),
        itemStyle: { color: delayColour(r.arrival_delay ?? r.departure_delay ?? 0) },
      })),
      lineStyle: { width: 2.6, color: PALETTE.violet },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(139,124,246,.34)' },
          { offset: 1, color: 'rgba(139,124,246,.02)' },
        ]),
      },
      markLine: {
        silent: true, symbol: 'none',
        data: [{ yAxis: 6 }],   // DB's 6-minute punctuality threshold.
        lineStyle: { color: 'rgba(255,176,32,.6)', type: 'dashed' },
        label: { formatter: 'punctuality threshold', color: PALETTE.mute, fontSize: 10 },
      },
    }],
  });
}

function renderWorstTrips(rows) {
  const tbody = document.getElementById('worst-trips');
  tbody.innerHTML = rows.map((r) => `
    <tr data-trip="${r.trip_id}" class="${r.trip_id === selectedTrip ? 'active' : ''}">
      <td><span class="badge ${r.route_category || ''}">${r.route_category || '—'}</span></td>
      <td class="route-cell">${r.route_name || r.trip_id}</td>
      <td class="delay-cell ${delayClass(r.max_delay_seconds)}">+${Math.round(minutes(r.max_delay_seconds))}′</td>
    </tr>`).join('');

  tbody.querySelectorAll('tr').forEach((row) => {
    row.addEventListener('click', () => selectTrip(row.dataset.trip));
  });

  // Keep showing something useful before the user has picked anything.
  if (!selectedTrip && rows.length) selectTrip(rows[0].trip_id);
}

async function selectTrip(tripId) {
  selectedTrip = tripId;
  document.querySelectorAll('.trips tr').forEach((row) => {
    row.classList.toggle('active', row.dataset.trip === tripId);
  });
  try {
    const response = await fetch(`/api/trips/${encodeURIComponent(tripId)}/propagation`);
    if (!response.ok) return;
    const rows = await response.json();
    renderPropagation(rows, `${rows[0]?.route_category || ''} ${tripId}`.trim());
  } catch (err) {
    console.warn('propagation fetch failed', err);
  }
}

// ---------------------------------------------------------------------------
// live wiring
// ---------------------------------------------------------------------------

function applyPayload(data) {
  const p = data.summary.punctuality;
  setCounter('punctuality', p.punctuality_pct, 1);
  setCounter('meandelay', minutes(p.mean_delay_seconds), 1);
  setCounter('trips', p.trips);
  setCounter('maxdelay', Math.round(minutes(p.max_delay_seconds)));
  setCounter('observations', data.summary.observations_stored);

  const cancelled = data.cancellations;
  setCounter('skipped', cancelled.skipped_stops);
  document.getElementById('skipped-sub').textContent =
    `${cancelled.skipped_pct}% of calls · ${cancelled.affected_trips} services`;
  document.getElementById('punct-bar').style.width = `${p.punctuality_pct}%`;

  const source = data.summary.source;
  document.getElementById('source-label').textContent = source.label;
  document.getElementById('lineage').classList.toggle('official', source.official);
  document.getElementById('feed-clock').textContent = clockTime(data.summary.feed.feed_timestamp);

  const feed = data.summary.feed;
  document.getElementById('feed-stats').textContent =
    `poll ${feed.duration_ms} ms · ${feed.entity_count.toLocaleString()} entities · ` +
    `${feed.long_distance_trips} long-distance · ${feed.rows_written} rows`;

  renderTimeseries(data.timeseries);
  renderMap(data.network);
  renderDistribution(data.distribution);
  renderStations(data.stations);
  renderCategories(data.categories);
  renderWorstTrips(data.worst_trips);
}

function setStatus(state, text) {
  const el = document.getElementById('status');
  el.className = `status ${state}`;
  document.getElementById('status-text').textContent = text;
}

/* Paint immediately from REST, then let the socket take over. Waiting for the
 * first push left every chart blank on load, and blank forever wherever
 * WebSockets are blocked. */
async function paintInitialState() {
  try {
    applyPayload(await (await fetch('/api/dashboard')).json());
  } catch (err) {
    console.warn('initial dashboard fetch failed', err);
  }
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.onopen = () => setStatus('live', 'live');
  socket.onmessage = (event) => {
    try {
      applyPayload(JSON.parse(event.data));
    } catch (err) {
      console.error('bad payload', err);
    }
  };
  // Retry rather than leaving a dead dashboard on screen after a restart.
  socket.onclose = () => {
    setStatus('down', 'reconnecting');
    setTimeout(connect, 3000);
  };
  socket.onerror = () => socket.close();
}

async function loadStationGeo() {
  try {
    stationGeo = await (await fetch('/api/stations/geo')).json();
  } catch (err) {
    console.warn('station geometry unavailable', err);
  }
}

async function loadModelInfo() {
  try {
    const info = await (await fetch('/api/model')).json();
    // The fold spread is shown deliberately: a single-split number on this
    // sample size moved by several points run to run.
    document.getElementById('model-info').textContent = info.trained
      ? `Delay model (${info.cv_folds}-fold): MAE ${info.mae_seconds}±${info.mae_std_seconds}s ` +
        `vs persistence ${info.baseline_mae_seconds}s (${info.improvement_pct > 0 ? '+' : ''}${info.improvement_pct}%) · ` +
        `RMSE ${info.rmse_seconds}s vs ${info.baseline_rmse_seconds}s · ${info.samples} samples`
      : 'Delay model: not trained';
  } catch (err) {
    console.warn('model info unavailable', err);
  }
}

window.addEventListener('resize', () => Object.values(charts).forEach((c) => c.resize()));

loadStationGeo().then(paintInitialState).then(connect);
loadModelInfo();
setInterval(loadModelInfo, 60000);
