function fmt(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "number") return `${value.toLocaleString()}${suffix}`;
  return `${value}${suffix}`;
}

function when(value) {
  return value ? new Date(value).toLocaleString() : "--";
}

function cell(row, value) {
  const td = document.createElement("td");
  td.textContent = value;
  row.append(td);
  return td;
}

function messageRow(rows, message) {
  rows.replaceChildren();
  const row = document.createElement("tr");
  const td = cell(row, message);
  td.colSpan = 6;
  rows.append(row);
}

function renderEvents(rows, events) {
  rows.replaceChildren();
  if (!events.length) {
    messageRow(rows, "No events logged for this aircraft.");
    return;
  }

  for (const event of events) {
    const row = document.createElement("tr");
    cell(row, when(event.seen_at));
    cell(row, event.callsign || "--");
    cell(row, fmt(event.altitude_ft, " ft"));
    cell(row, event.distance_mi === null || event.distance_mi === undefined ? "--" : `${Number(event.distance_mi).toFixed(2)} mi`);
    cell(row, fmt(event.speed_kt, " kt"));
    cell(row, event.event_type || "--");
    rows.append(row);
  }
}

async function loadAircraft() {
  const hex = decodeURIComponent(window.location.pathname.split("/").pop() || "");
  document.querySelector("#aircraftHex").textContent = hex || "--";
  const rows = document.querySelector("#aircraftRows");
  try {
    const response = await fetch(`/api/aircraft/${encodeURIComponent(hex)}`);
    if (!response.ok) throw new Error("missing");
    const payload = await response.json();
    const aircraft = payload.aircraft || {};
    document.querySelector("#aircraftHex").textContent = aircraft.registration || aircraft.hex || hex;
    document.querySelector("#aircraftMeta").textContent = [
      aircraft.hex,
      aircraft.aircraft_type,
      aircraft.operator,
      aircraft.first_seen ? `first seen ${when(aircraft.first_seen)}` : null,
    ].filter(Boolean).join(" | ") || "Local ADS-B aircraft";
    document.querySelector("#aircraftSightings").textContent = fmt(aircraft.total_sightings || 0);
    document.querySelector("#aircraftLowest").textContent = fmt(aircraft.lowest_altitude_ft, " ft");
    document.querySelector("#aircraftClosest").textContent = aircraft.lowest_distance_mi === null || aircraft.lowest_distance_mi === undefined
      ? "--"
      : `${Number(aircraft.lowest_distance_mi).toFixed(2)} mi`;
    renderEvents(rows, payload.events || []);
  } catch (error) {
    document.querySelector("#aircraftMeta").textContent = "Aircraft has not been logged yet.";
    messageRow(rows, "No events logged for this aircraft.");
  }
}

loadAircraft();
