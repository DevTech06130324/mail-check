/* URL state keeps dashboard filters shareable and restorable. */
function dashboardReadFilters(search, browserTimezone) {
  const p = new URLSearchParams(search);
  const oneOf = (key, values, fallback) => values.includes(p.get(key)) ? p.get(key) : fallback;
  const custom = p.has("start") && p.has("end");
  return {
    days: custom ? "custom" : oneOf("days", ["7", "30", "90"], "30"),
    start: p.get("start") || "", end: p.get("end") || "",
    interval: oneOf("interval", ["day", "week"], "day"),
    tz: p.get("tz") || browserTimezone || "UTC", account: p.get("account") || "",
    categories: [...new Set(p.getAll("category"))],
    status: oneOf("status", ["all", "pending", "done"], "all"),
    action: oneOf("action", ["all", "yes", "no"], "all"),
  };
}

function dashboardParams(f) {
  const p = new URLSearchParams();
  if (f.days === "custom") { p.set("start", f.start); p.set("end", f.end); }
  else p.set("days", f.days);
  p.set("interval", f.interval);
  p.set("tz", f.tz);
  if (f.account) p.set("account", f.account);
  f.categories.forEach((category) => p.append("category", category));
  p.set("status", f.status); p.set("action", f.action);
  return p;
}

function dashboardDateRangeValid(start, end) {
  const date = (value) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? parsed.getTime() : NaN;
  };
  const a = date(start), b = date(end);
  return Number.isFinite(a) && Number.isFinite(b) && b >= a && (b - a) / 86400000 < 90;
}

document.addEventListener("DOMContentLoaded", () => {
  if (!document.querySelector("[data-dashboard]")) return;
  const el = (id) => document.getElementById(id);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  let filters = dashboardReadFilters(location.search, timezone);
  let data = null, controller = null, sequence = 0, chart = null;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const categoryColors = ["#0071e3", "#c9252d", "#8a5a00", "#7e22ce", "#1d7f34", "#008a91", "#9e477f", "#787857", "#686f8d", "#6e6e73"];
  const format = (n) => Number(n || 0).toLocaleString();
  const escape = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
  const displayDate = (date) => {
    if (!date) return "Unknown date";
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(`${date}T12:00:00Z`));
  };

  function syncControls() {
    for (const [id, key] of [["dashboard-range", "days"], ["dashboard-interval", "interval"], ["dashboard-account", "account"], ["dashboard-status", "status"], ["dashboard-action", "action"], ["dashboard-start", "start"], ["dashboard-end", "end"]]) el(id).value = filters[key];
    el("dashboard-custom").hidden = filters.days !== "custom";
    document.querySelectorAll('#dashboard-filters [name="category"]').forEach((box) => { box.checked = filters.categories.includes(box.value); });
    el("dashboard-category-count").textContent = filters.categories.length ? `${filters.categories.length} selected` : "All";
    el("dashboard-advanced-state").textContent = [filters.status !== "all" && `Status: ${filters.status === "done" ? "Done" : "Pending"}`, filters.action !== "all" && `Action required: ${filters.action === "yes" ? "Yes" : "No"}`].filter(Boolean).join(" · ");
    el("dashboard-timezone").textContent = filters.tz;
  }

  function writeURL(replace = false) {
    const url = `${location.pathname}?${dashboardParams(filters)}`;
    if (`${location.pathname}${location.search}` !== url) history[replace ? "replaceState" : "pushState"]({}, "", url);
  }

  function showError(message) {
    el("dashboard-error").hidden = false;
    el("dashboard-error").querySelector("span").textContent = message;
  }

  function change(updates, { replace = false } = {}) {
    filters = { ...filters, ...updates };
    syncControls(); writeURL(replace); load();
  }

  async function load() {
    controller?.abort();
    controller = new AbortController();
    const current = ++sequence;
    el("dashboard-loading").hidden = false;
    el("dashboard-loading").textContent = data ? "Updating dashboard… Previous results remain visible." : "Loading dashboard…";
    el("dashboard-error").hidden = true;
    el("dashboard-stats").setAttribute("aria-busy", "true");
    try {
      const response = await fetch(`/api/dashboard?${dashboardParams(filters)}`, { signal: controller.signal });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.error || `Request failed (${response.status})`);
      if (current !== sequence) return;
      data = result;
      render();
    } catch (error) {
      if (error.name !== "AbortError" && current === sequence) showError(`${error.message}${data ? " Previous results are shown." : ""}`);
    } finally {
      if (current === sequence) { el("dashboard-loading").hidden = true; el("dashboard-stats").setAttribute("aria-busy", "false"); }
    }
  }

  function activityIntervals() {
    if (filters.interval === "day") return data.daily.map((day) => ({ ...day, start: day.date, end: day.date }));
    const weeks = new Map();
    for (const day of data.daily) {
      const date = new Date(`${day.date}T00:00:00Z`);
      const mondayOffset = (date.getUTCDay() + 6) % 7;
      date.setUTCDate(date.getUTCDate() - mondayOffset);
      const monday = date.toISOString().slice(0, 10);
      let week = weeks.get(monday);
      if (!week) {
        week = { start: day.date, end: day.date, counts: Object.fromEntries(data.categories.map((category) => [category.name, 0])), total: 0, partial: false };
        weeks.set(monday, week);
      }
      week.end = day.date;
      week.total += day.total;
      week.partial ||= day.partial;
      for (const category of data.categories) week.counts[category.name] += day.counts[category.name] || 0;
    }
    return [...weeks.values()];
  }

  function render() {
    for (const [id, key] of [["dashboard-total", "total"], ["dashboard-pending", "pending_attention"], ["dashboard-completed", "completed"], ["dashboard-unclassified", "unclassified"]]) el(id).textContent = format(data.summary[key]);
    el("dashboard-retryable").textContent = `${format(data.summary.retryable)} retryable`;
    const h = data.history || {};
    const notes = [`${displayDate(data.filters.start)} – ${displayDate(data.filters.end)} · ${filters.tz}`];
    if (h.collection_start_date) notes.push(`Collection started ${displayDate(h.collection_start_date)}`);
    if (h.earliest_received_date) notes.push(`Earliest stored message ${displayDate(h.earliest_received_date)}`);
    if (h.retention_days) notes.push(`${h.retention_days}-day retention`);
    if (data.daily.some((day) => day.partial)) notes.push("Intervals marked * have partial collection history; an empty interval may have no stored history.");
    el("dashboard-history").textContent = notes.join(" · ");
    renderChart(); renderCategoryBars(); renderTable();
  }

  function renderChart() {
    const fallback = el("dashboard-chart-fallback");
    if (typeof Chart !== "function") { fallback.hidden = false; el("dashboard-chart").parentElement.hidden = true; el("dashboard-table-details").open = true; return; }
    const intervals = activityIntervals();
    const palette = getComputedStyle(document.documentElement);
    const text = palette.getPropertyValue("--text-2").trim();
    const datasets = data.categories.map((category, index) => ({ label: category.label, data: intervals.map((interval) => interval.counts[category.name] || 0), backgroundColor: categoryColors[index % categoryColors.length], borderWidth: 0 }));
    try {
      chart?.destroy();
      chart = new Chart(el("dashboard-chart"), {
        type: "bar", data: { labels: intervals.map((interval) => interval.start), datasets },
        options: { responsive: true, maintainAspectRatio: false, animation: reducedMotion.matches ? false : { duration: 180 },
          scales: { x: { stacked: true, grid: { display: false }, ticks: { color: text, maxTicksLimit: 10, callback(value) { return displayDate(this.getLabelForValue(value)).replace(/, \d{4}$/, ""); } } }, y: { stacked: true, beginAtZero: true, ticks: { color: text, precision: 0 }, grid: { color: palette.getPropertyValue("--separator").trim() } } },
          plugins: { legend: { display: false }, tooltip: { callbacks: { title(items) { const interval = intervals[items[0].dataIndex]; const title = interval.start === interval.end ? displayDate(interval.start) : `${displayDate(interval.start)} – ${displayDate(interval.end)}`; return `${title}${interval.partial ? " · Partial history" : ""}`; } } } },
        },
      });
      fallback.hidden = true; el("dashboard-chart").parentElement.hidden = false;
    } catch (error) { chart = null; fallback.hidden = false; el("dashboard-chart").parentElement.hidden = true; el("dashboard-table-details").open = true; }
  }

  function renderCategoryBars() {
    const max = Math.max(1, ...data.categories.map((category) => category.count));
    el("dashboard-category-bars").innerHTML = data.categories.map((category, index) => `<button type="button" class="analytics-category-bar" data-category-filter="${escape(category.name)}" aria-pressed="${filters.categories.includes(category.name)}" aria-label="Filter by ${escape(category.label)}, ${format(category.count)} messages"><span class="analytics-bar-fill" style="width:${Math.max(0, category.count / max * 100)}%"></span><span class="analytics-bar-label"><span class="analytics-bar-name"><span class="analytics-color-swatch" style="background:${categoryColors[index % categoryColors.length]}" aria-hidden="true"></span>${escape(category.label)}</span><strong>${format(category.count)}</strong></span></button>`).join("");
  }

  function renderTable() {
    const intervals = activityIntervals();
    const unit = filters.interval === "week" ? "Week" : "Day";
    const table = el("dashboard-daily-table");
    table.querySelector("thead").innerHTML = `<tr><th scope="col">${unit}</th>${data.categories.map((c) => `<th scope="col">${escape(c.label)}</th>`).join("")}<th scope="col">Total</th></tr>`;
    table.querySelector("tbody").innerHTML = intervals.map((interval) => {
      const label = interval.start === interval.end ? displayDate(interval.start) : `${displayDate(interval.start)} – ${displayDate(interval.end)}`;
      return `<tr><th scope="row">${escape(label)}${interval.partial ? '<span title="Partial collection history"> *</span>' : ""}</th>${data.categories.map((category) => `<td>${format(interval.counts[category.name])}</td>`).join("")}<td>${format(interval.total)}</td></tr>`;
    }).join("");
  }

  el("dashboard-filters").addEventListener("submit", (event) => event.preventDefault());
  for (const [id, key] of [["dashboard-account", "account"], ["dashboard-status", "status"], ["dashboard-action", "action"], ["dashboard-interval", "interval"]]) el(id).addEventListener("change", () => change({ [key]: el(id).value }));
  el("dashboard-range").addEventListener("change", () => {
    if (el("dashboard-range").value === "custom") {
      el("dashboard-custom").hidden = false;
      if (data) { el("dashboard-start").value = data.filters.start; el("dashboard-end").value = data.filters.end; }
      el("dashboard-start").focus();
    } else change({ days: el("dashboard-range").value, start: "", end: "" });
  });
  el("dashboard-apply-dates").addEventListener("click", () => {
    const start = el("dashboard-start").value, end = el("dashboard-end").value;
    if (!dashboardDateRangeValid(start, end)) { showError("Choose a valid start and end date spanning no more than 90 days, including both dates."); el("dashboard-start").focus(); return; }
    change({ days: "custom", start, end });
  });
  document.querySelectorAll('#dashboard-filters [name="category"]').forEach((box) => box.addEventListener("change", () => change({ categories: [...document.querySelectorAll('#dashboard-filters [name="category"]:checked')].map((b) => b.value) })));
  el("dashboard-all-categories").addEventListener("click", () => change({ categories: [] }));
  el("dashboard-reset").addEventListener("click", () => { filters = dashboardReadFilters("", timezone); syncControls(); writeURL(); load(); });
  for (const id of ["dashboard-refresh", "dashboard-retry"]) el(id).addEventListener("click", load);
  el("analytics").addEventListener("click", (event) => {
    const category = event.target.closest("[data-category-filter]");
    if (category) change({ categories: [category.dataset.categoryFilter] });
  });
  window.addEventListener("popstate", () => { filters = dashboardReadFilters(location.search, timezone); syncControls(); load(); });
  window.addEventListener("mailcheck:job-finished", (event) => { event.preventDefault(); load(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (data) renderChart(); });
  syncControls(); writeURL(true); load();
});
