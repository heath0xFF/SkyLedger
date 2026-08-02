const state = {
  payload: null,
  radarAngle: 0,
  ws: null,
  mapKey: "",
  mapMeta: null,
  receiverStarting: false,
};

const els = {
  clock: document.querySelector("#clock"),
  receiverStatus: document.querySelector("#receiverStatus"),
  lastUpdate: document.querySelector("#lastUpdate"),
  dashboardTitle: document.querySelector("#dashboardTitle"),
  homeName: document.querySelector("#homeName"),
  liveMode: document.querySelector("#liveMode"),
  countdownMode: document.querySelector("#countdownMode"),
  revealMode: document.querySelector("#revealMode"),
  radarCanvas: document.querySelector("#radarCanvas"),
  mapView: document.querySelector("#mapView"),
  mapTiles: document.querySelector("#mapTiles"),
  mapZoomLabel: document.querySelector("#mapZoomLabel"),
  closestDistance: document.querySelector("#closestDistance"),
  statTotalAircraft: document.querySelector("#statTotalAircraft"),
  statCurrentAircraft: document.querySelector("#statCurrentAircraft"),
  statTotalFlyovers: document.querySelector("#statTotalFlyovers"),
  statTodayFlyovers: document.querySelector("#statTodayFlyovers"),
  statLowestFlyover: document.querySelector("#statLowestFlyover"),
  statMaxDistance: document.querySelector("#statMaxDistance"),
  closestCallsign: document.querySelector("#closestCallsign"),
  closestIdentifier: document.querySelector("#closestIdentifier"),
  closestSightings: document.querySelector("#closestSightings"),
  closestRoute: document.querySelector("#closestRoute"),
  closestAltitude: document.querySelector("#closestAltitude"),
  closestSpeed: document.querySelector("#closestSpeed"),
  closestHeading: document.querySelector("#closestHeading"),
  countdownNumber: document.querySelector("#countdownNumber"),
  countdownCallsign: document.querySelector("#countdownCallsign"),
  countdownDirection: document.querySelector("#countdownDirection"),
  countdownDistance: document.querySelector("#countdownDistance"),
  countdownSpeed: document.querySelector("#countdownSpeed"),
  revealCallsign: document.querySelector("#revealCallsign"),
  revealAltitude: document.querySelector("#revealAltitude"),
  revealBadge: document.querySelector("#revealBadge"),
  revealDistance: document.querySelector("#revealDistance"),
  revealSpeed: document.querySelector("#revealSpeed"),
  revealHeading: document.querySelector("#revealHeading"),
  revealVertical: document.querySelector("#revealVertical"),
  revealAircraft: document.querySelector("#revealAircraft"),
  revealHistory: document.querySelector("#revealHistory"),
  revealHex: document.querySelector("#revealHex"),
  revealRoute: document.querySelector("#revealRoute"),
  revealTimer: document.querySelector("#revealTimer"),
};

function fmtNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString();
}

function fmtAltitude(value) {
  return value === null || value === undefined ? "--" : `${fmtNumber(value)} ft`;
}

function fmtDistance(value) {
  return value === null || value === undefined ? "--" : `${Number(value).toFixed(2)} mi`;
}

function fmtShortDistance(value) {
  const distance = Number(value);
  if (!Number.isFinite(distance)) return null;
  return `${distance.toFixed(distance < 1 ? 2 : 1)} mi`;
}

function fmtSpeed(value) {
  return value === null || value === undefined ? "--" : `${fmtNumber(value)} kt`;
}

function fmtHeading(value) {
  return value === null || value === undefined ? "--" : `${Math.round(value)} deg`;
}

function fmtFrequentFlyer(count) {
  if (count === undefined || count === null || Number.isNaN(Number(count))) return "--";
  const num = Number(count);
  if (num === 0) return "New plane! 🆕";
  if (num === 1) return "First logged pass! 🔄";
  if (num < 5) return `Frequent flyer (${num}x) ⚡`;
  return `Local regular (${num}x) 👑`;
}

function label(ac) {
  return ac?.callsign || ac?.registration || ac?.hex || "Unknown";
}

function identifierLabel(ac) {
  const callsign = ac?.callsign;
  const registration = ac?.registration;
  if (callsign && registration) return `${callsign} / ${registration}`;
  return callsign || registration || ac?.hex || "--";
}

function setMode(mode) {
  for (const panel of [els.liveMode, els.countdownMode, els.revealMode]) {
    panel.classList.remove("active");
  }
  if (mode === "countdown") els.countdownMode.classList.add("active");
  else if (mode === "reveal") els.revealMode.classList.add("active");
  else els.liveMode.classList.add("active");
}

function updateClock() {
  els.clock.textContent = new Date().toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

function render(payload) {
  state.payload = payload;
  const config = payload.config || {};
  const status = payload.status || {};
  const stats = payload.stats_today || {};
  const summary = payload.stats_total || {};
  const closest = payload.closest_aircraft;

  document.title = config.dashboard_title || "SkyLedger";
  els.dashboardTitle.textContent = config.dashboard_title || "SkyLedger";
  els.homeName.textContent = config.home_name || "SkyLedger Flight Command Center";

  renderReceiverStatus(status);
  els.lastUpdate.textContent = status.last_poll_at ? `Updated ${new Date(status.last_poll_at).toLocaleTimeString()}` : "Waiting for data";

  setMode(payload.mode);
  renderLive(payload, stats, summary, closest);
  renderMap(payload);
  if (payload.mode === "countdown") renderCountdown(payload.focus || {});
  if (payload.mode === "reveal") renderReveal(payload.focus || {});
}

function renderReceiverStatus(status = {}) {
  const online = Boolean(status.receiver_online);
  const starting = state.receiverStarting && !online;
  els.receiverStatus.textContent = online ? "Receiver online" : starting ? "Starting receiver..." : "Receiver offline";
  els.receiverStatus.className = `status-pill ${online ? "online" : "offline"}${starting ? " loading" : ""}`;
  els.receiverStatus.disabled = online || starting;
  els.receiverStatus.title = online ? "Receiver is online" : "Start the local receiver";
  els.receiverStatus.setAttribute("aria-label", online ? "Receiver online" : "Receiver offline. Start local receiver.");
}

function renderLive(payload, stats, summary, closest) {
  const live = payload.live_aircraft || [];
  const currentMapped = live.filter((ac) => Number.isFinite(Number(ac.lat)) && Number.isFinite(Number(ac.lon))).length;
  els.closestDistance.textContent = closest ? fmtDistance(closest.distance_mi) : "--";
  els.statTotalAircraft.textContent = fmtNumber(summary.total_aircraft || 0);
  els.statCurrentAircraft.textContent = fmtNumber(currentMapped);
  els.statTotalFlyovers.textContent = fmtNumber(summary.total_flyovers || 0);
  els.statTodayFlyovers.textContent = fmtNumber(stats.total_flyovers || 0);
  els.statLowestFlyover.textContent = fmtAltitude(summary.lowest_flyover_ft);
  els.statMaxDistance.textContent = fmtDistance(summary.max_distance_mi);
  els.closestCallsign.textContent = closest ? label(closest) : "No aircraft nearby";
  els.closestIdentifier.textContent = closest ? identifierLabel(closest) : "--";
  els.closestSightings.textContent = closest ? fmtFrequentFlyer(closest.total_sightings) : "--";
  els.closestRoute.textContent = closest?.route_from && closest?.route_to
    ? `${closest.route_from} → ${closest.route_to}`
    : "Route unknown";
  els.closestAltitude.textContent = closest ? fmtAltitude(closest.altitude_ft) : "--";
  els.closestSpeed.textContent = closest ? fmtSpeed(closest.speed_kt) : "--";
  els.closestHeading.textContent = closest ? fmtHeading(closest.heading) : "--";
}

function renderCountdown(focus) {
  els.countdownNumber.textContent = Math.max(0, focus.seconds_remaining || 0);
  els.countdownCallsign.textContent = focus.callsign || focus.hex || "Inbound aircraft";
  els.countdownDirection.textContent = focus.direction || "Approaching";
  els.countdownDistance.textContent = fmtDistance(focus.distance_mi);
  els.countdownSpeed.textContent = fmtSpeed(focus.speed_kt);
}

function renderReveal(focus) {
  els.revealCallsign.textContent = focus.callsign || focus.hex || "Unknown";
  els.revealAltitude.textContent = fmtAltitude(focus.altitude_ft);
  const badges = [];
  if (focus.is_new_aircraft) badges.push("New aircraft over SkyLedger");
  if (focus.is_new_lowest) badges.push("Lowest pass recorded");
  if (!badges.length) badges.push(`You've seen this aircraft ${fmtNumber(focus.total_sightings || 1)} time(s)`);
  els.revealBadge.textContent = badges.join(" | ");
  els.revealDistance.textContent = fmtDistance(focus.distance_mi);
  els.revealSpeed.textContent = fmtSpeed(focus.speed_kt);
  els.revealHeading.textContent = fmtHeading(focus.heading);
  els.revealVertical.textContent = focus.vertical_rate_fpm === null || focus.vertical_rate_fpm === undefined ? "--" : `${fmtNumber(focus.vertical_rate_fpm)} fpm`;
  els.revealAircraft.textContent = [focus.registration, focus.aircraft_type, focus.operator].filter(Boolean).join(" | ") || "Local ADS-B only";
  els.revealHistory.textContent = `Total sightings ${fmtNumber(focus.total_sightings || 1)}; lowest ${fmtAltitude(focus.lowest_altitude_ft)}`;
  els.revealHex.textContent = `hex ${focus.hex || "--"}`;
  els.revealRoute.textContent = focus.route_from && focus.route_to ? `${focus.route_from} to ${focus.route_to}` : "Route unknown";
  els.revealTimer.textContent = `${Math.max(0, focus.seconds_remaining || 0)}s`;
}

function drawRadar() {
  const canvas = els.radarCanvas;
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.floor(rect.width * scale));
  const height = Math.max(320, Math.floor(rect.height * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }

  const cx = width / 2;
  const cy = height / 2;
  const radius = overlayRadarRadius(width / scale, height / scale) * scale;
  ctx.clearRect(0, 0, width, height);
  ctx.lineWidth = 1 * scale;
  ctx.strokeStyle = "rgba(110, 231, 255, 0.34)";
  ctx.fillStyle = "rgba(97, 244, 168, 0.08)";

  for (let ring = 1; ring <= 4; ring += 1) {
    ctx.beginPath();
    ctx.arc(cx, cy, (radius * ring) / 4, 0, Math.PI * 2);
    ctx.stroke();
  }
  for (let spoke = 0; spoke < 12; spoke += 1) {
    const angle = (spoke / 12) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius);
    ctx.stroke();
  }

  state.radarAngle = (state.radarAngle + 0.012) % (Math.PI * 2);
  const sweep = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
  sweep.addColorStop(0, "rgba(97, 244, 168, 0.2)");
  sweep.addColorStop(1, "rgba(97, 244, 168, 0)");
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, radius, state.radarAngle - 0.18, state.radarAngle);
  ctx.closePath();
  ctx.fillStyle = sweep;
  ctx.fill();
  ctx.restore();

  ctx.fillStyle = "#61f4a8";
  ctx.beginPath();
  ctx.arc(cx, cy, 5 * scale, 0, Math.PI * 2);
  ctx.fill();

  drawMapOverlay(ctx, scale, width / scale, height / scale);
  requestAnimationFrame(drawRadar);
}

function renderMap(payload) {
  const config = payload.config || {};
  const homeLat = Number(config.home_lat);
  const homeLon = Number(config.home_lon);
  const zoom = clamp(Math.round(Number(config.map_zoom_level ?? 13)), 0, 19);
  const tileTemplate = config.map_tile_url || "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const rect = els.mapView?.getBoundingClientRect();

  if (!Number.isFinite(homeLat) || !Number.isFinite(homeLon) || !rect || rect.width < 20 || rect.height < 20) {
    state.mapMeta = null;
    return;
  }

  els.mapZoomLabel.textContent = `Zoom ${zoom}`;
  const width = Math.round(rect.width);
  const height = Math.round(rect.height);
  const center = latLonToWorld(homeLat, homeLon, zoom);
  state.mapMeta = { homeLat, homeLon, zoom, center, width, height };

  const key = [homeLat.toFixed(6), homeLon.toFixed(6), zoom, width, height, tileTemplate].join("|");
  if (key === state.mapKey) return;
  state.mapKey = key;

  const left = center.x - width / 2;
  const top = center.y - height / 2;
  const startX = Math.floor(left / 256);
  const endX = Math.floor((left + width) / 256);
  const startY = Math.floor(top / 256);
  const endY = Math.floor((top + height) / 256);
  const tileCount = 2 ** zoom;
  const tiles = document.createDocumentFragment();

  for (let tileY = startY; tileY <= endY; tileY += 1) {
    if (tileY < 0 || tileY >= tileCount) continue;
    for (let tileX = startX; tileX <= endX; tileX += 1) {
      const wrappedX = ((tileX % tileCount) + tileCount) % tileCount;
      const x = Math.round(tileX * 256 - left);
      const y = Math.round(tileY * 256 - top);
      const url = tileTemplate
        .replaceAll("{z}", String(zoom))
        .replaceAll("{x}", String(wrappedX))
        .replaceAll("{y}", String(tileY));
      const tile = document.createElement("img");
      tile.src = url;
      tile.alt = "";
      tile.style.left = `${x}px`;
      tile.style.top = `${y}px`;
      tile.loading = "lazy";
      tile.referrerPolicy = "no-referrer";
      tile.addEventListener("error", () => tile.classList.add("tile-error"));
      tiles.append(tile);
    }
  }

  els.mapTiles.replaceChildren(tiles);
}

function drawMapOverlay(ctx, scale, cssWidth, cssHeight) {
  if (!state.mapMeta) return;

  ctx.save();
  ctx.scale(scale, scale);

  const homeX = cssWidth / 2;
  const homeY = cssHeight / 2;
  drawMapRange(ctx, homeX, homeY, state.mapMeta);

  for (const ac of state.payload?.live_aircraft || []) {
    if (!Number.isFinite(Number(ac.lat)) || !Number.isFinite(Number(ac.lon))) continue;
    const point = mapPoint(ac.lat, ac.lon, state.mapMeta, cssWidth, cssHeight);
    if (!point || point.x < -40 || point.x > cssWidth + 40 || point.y < -40 || point.y > cssHeight + 40) continue;
    drawAircraftMarker(ctx, point.x, point.y, ac);
  }

  ctx.restore();
}

function overlayRadarRadius(width, height) {
  if (!state.mapMeta) {
    return Math.min(width, height) * 0.46;
  }

  const trackingRadius = Number(state.payload?.config?.tracking_radius_miles);
  if (!Number.isFinite(trackingRadius) || trackingRadius <= 0) {
    return Math.min(width, height) * 0.48;
  }

  const geographicRadius = mapRadiusPixels(trackingRadius, state.mapMeta);
  const minimumRadius = Math.min(width, height) * 0.42;
  const maximumRadius = Math.max(width, height);
  return Math.max(minimumRadius, Math.min(geographicRadius, maximumRadius));
}

function drawMapRange(ctx, homeX, homeY, meta) {
  const config = state.payload?.config || {};
  const ranges = [
    { miles: Number(config.alert_radius_miles), color: "rgba(255, 200, 87, 0.44)" },
    { miles: Number(config.guess_trigger_radius_miles), color: "rgba(97, 244, 168, 0.34)" },
    { miles: Number(config.tracking_radius_miles), color: "rgba(110, 231, 255, 0.22)" },
  ].filter((range) => Number.isFinite(range.miles) && range.miles > 0);

  for (const range of ranges) {
    const radius = mapRadiusPixels(range.miles, meta);
    ctx.beginPath();
    ctx.arc(homeX, homeY, radius, 0, Math.PI * 2);
    ctx.strokeStyle = range.color;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
}

function drawAircraftMarker(ctx, x, y, ac) {
  const heading = Number(ac.heading || 0) * (Math.PI / 180);
  const config = state.payload?.config || {};
  const markerColor = aircraftMarkerColor(ac, config);
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(heading);
  ctx.beginPath();
  ctx.moveTo(0, -10);
  ctx.lineTo(7, 8);
  ctx.lineTo(0, 4);
  ctx.lineTo(-7, 8);
  ctx.closePath();
  ctx.fillStyle = markerColor;
  ctx.shadowColor = `${markerColor}80`;
  ctx.shadowBlur = 10;
  ctx.fill();
  ctx.restore();

  ctx.shadowBlur = 0;
  const nameText = label(ac);
  const distanceText = fmtShortDistance(ac.distance_mi);
  const labelX = x + 10;
  const labelY = y - 12;

  ctx.textBaseline = "top";
  ctx.lineJoin = "round";
  ctx.strokeStyle = "rgba(5, 7, 10, 0.9)";
  ctx.lineWidth = 3;
  ctx.font = "700 12px system-ui, sans-serif";
  ctx.strokeText(nameText, labelX, labelY);
  ctx.fillStyle = "rgba(245, 248, 251, 0.94)";
  ctx.fillText(nameText, labelX, labelY);

  if (distanceText) {
    ctx.font = "700 10px system-ui, sans-serif";
    ctx.strokeText(distanceText, labelX, labelY + 15);
    ctx.fillStyle = "rgba(180, 204, 214, 0.92)";
    ctx.fillText(distanceText, labelX, labelY + 15);
  }
  ctx.textBaseline = "alphabetic";
}

function aircraftMarkerColor(ac, config) {
  const altitude = ac.altitude_ft === null || ac.altitude_ft === undefined ? NaN : Number(ac.altitude_ft);
  const threshold = Number(config.max_alert_altitude_ft ?? 10000);
  const lowColor = validHexColor(config.aircraft_marker_low_color, "#61f4a8");
  const defaultColor = validHexColor(config.aircraft_marker_default_color, "#6ee7ff");
  if (Number.isFinite(altitude) && Number.isFinite(threshold) && altitude <= threshold) {
    return lowColor;
  }
  return defaultColor;
}

function validHexColor(value, fallback) {
  return /^#[0-9a-f]{6}$/i.test(String(value || "")) ? value : fallback;
}

function mapRadiusPixels(miles, meta) {
  const latOffset = miles / 69;
  const target = latLonToWorld(meta.homeLat + latOffset, meta.homeLon, meta.zoom);
  return Math.abs(target.y - meta.center.y);
}

function mapPoint(lat, lon, meta, width, height) {
  const world = latLonToWorld(Number(lat), Number(lon), meta.zoom);
  const worldWidth = 256 * (2 ** meta.zoom);
  let dx = world.x - meta.center.x;
  if (dx > worldWidth / 2) dx -= worldWidth;
  if (dx < -worldWidth / 2) dx += worldWidth;
  return {
    x: width / 2 + dx,
    y: height / 2 + (world.y - meta.center.y),
  };
}

function latLonToWorld(lat, lon, zoom) {
  const sinLat = Math.sin(clamp(lat, -85.05112878, 85.05112878) * (Math.PI / 180));
  const scale = 256 * (2 ** zoom);
  return {
    x: ((lon + 180) / 360) * scale,
    y: (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * scale,
  };
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function connect() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${protocol}://${window.location.host}/ws/live`);
  state.ws.onmessage = (event) => render(JSON.parse(event.data));
  state.ws.onclose = () => setTimeout(connect, 2000);
  state.ws.onerror = () => state.ws.close();
}

async function fetchLivePayload() {
  const response = await fetch("/api/aircraft/live");
  render(await response.json());
}

function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function startReceiver() {
  if (state.receiverStarting || Boolean(state.payload?.status?.receiver_online)) return;
  state.receiverStarting = true;
  renderReceiverStatus(state.payload?.status || {});
  els.lastUpdate.textContent = "Starting receiver...";

  try {
    const response = await fetch("/api/receiver/start", { method: "POST" });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      throw new Error(result.error || "Receiver start failed");
    }

    els.lastUpdate.textContent = result.already_running ? "Receiver already running; refreshing" : "Receiver starting; waiting for data";
    await delay(1500);
    await fetchLivePayload();
    if (!state.payload?.status?.receiver_online) {
      await delay(3500);
      await fetchLivePayload();
    }
  } catch (error) {
    els.lastUpdate.textContent = error?.message || "Receiver start failed";
  } finally {
    state.receiverStarting = false;
    renderReceiverStatus(state.payload?.status || {});
  }
}

async function pollFallback() {
  if (!state.payload || !state.ws || state.ws.readyState !== WebSocket.OPEN) {
    try {
      await fetchLivePayload();
    } catch {
      // The status pill already shows stale/offline state.
    }
  }
  setTimeout(pollFallback, 5000);
}

updateClock();
setInterval(updateClock, 1000);
els.receiverStatus.addEventListener("click", startReceiver);
window.addEventListener("resize", () => {
  state.mapKey = "";
  if (state.payload) renderMap(state.payload);
});
connect();
pollFallback();
drawRadar();
