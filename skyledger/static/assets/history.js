function fmt(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "number") return `${value.toLocaleString()}${suffix}`;
  return `${value}${suffix}`;
}

function when(value) {
  return value ? new Date(value).toLocaleString() : "--";
}

function flags(row) {
  const list = [];
  if (row.is_new_aircraft) list.push("new");
  if (row.is_new_lowest) list.push("lowest");
  if (row.was_alerted) list.push("alerted");
  if (row.was_revealed) list.push("revealed");
  return list.join(", ") || "--";
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
  td.colSpan = 7;
  rows.append(row);
}

function renderEvents(rows, events) {
  rows.replaceChildren();
  if (!events.length) {
    messageRow(rows, "No flyovers logged yet.");
    return;
  }

  for (const event of events) {
    const row = document.createElement("tr");
    cell(row, when(event.seen_at));

    const identity = document.createElement("td");
    const link = document.createElement("a");
    link.href = `/aircraft/${encodeURIComponent(event.hex || "")}`;
    link.textContent = event.callsign || event.hex || "--";
    identity.append(link);
    row.append(identity);

    cell(row, fmt(event.altitude_ft, " ft"));
    cell(row, event.distance_mi === null || event.distance_mi === undefined ? "--" : `${Number(event.distance_mi).toFixed(2)} mi`);
    cell(row, fmt(event.speed_kt, " kt"));
    cell(row, [event.registration, event.aircraft_type, event.operator].filter(Boolean).join(" | ") || event.hex || "--");
    cell(row, flags(event));
    rows.append(row);
  }
}

async function loadHistory() {
  const rows = document.querySelector("#historyRows");
  try {
    const response = await fetch("/api/history?limit=300");
    const events = await response.json();
    renderEvents(rows, events);
  } catch (error) {
    messageRow(rows, "Unable to load history.");
  }
}

loadHistory();
