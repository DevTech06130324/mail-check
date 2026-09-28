/* URL state is the source of truth; chart focus only narrows the explorer. */
function dashboardReadFilters(search, browserTimezone) {
  const p = new URLSearchParams(search);
  const oneOf = (key, values, fallback) => values.includes(p.get(key)) ? p.get(key) : fallback;
  const custom = p.has("start") && p.has("end");
  return {
    days: custom ? "custom" : oneOf("days", ["7", "30", "90"], "30"),
    start: p.get("start") || "", end: p.get("end") || "",
    tz: p.get("tz") || browserTimezone || "UTC", account: p.get("account") || "",
    categories: [...new Set(p.getAll("category"))],
    status: oneOf("status", ["all", "pending", "done"], "all"),
    action: oneOf("action", ["all", "yes", "no"], "all"), q: (p.get("q") || "").slice(0, 200),
    sort: oneOf("sort", ["newest", "oldest"], "newest"),
    page: Math.max(1, Math.floor(Number(p.get("page")) || 1)), page_size: 50,
    focus_date: p.get("focus_date") || "", focus_category: p.get("focus_category") || "",
  };
}

function dashboardParams(f) {
  const p = new URLSearchParams();
  if (f.days === "custom") { p.set("start", f.start); p.set("end", f.end); }
  else p.set("days", f.days);
  p.set("tz", f.tz);
  if (f.account) p.set("account", f.account);
  f.categories.forEach((category) => p.append("category", category));
  p.set("status", f.status); p.set("action", f.action);
  if (f.q) p.set("q", f.q);
  p.set("sort", f.sort); p.set("page", String(f.page)); p.set("page_size", "50");
  if (f.focus_date) p.set("focus_date", f.focus_date);
  if (f.focus_category) p.set("focus_category", f.focus_category);
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
  let data = null, controller = null, sequence = 0, searchTimer = null;
  let selected = null, readerController = null, readerSequence = 0, chart = null;
  let mobileScroll = 0, returnFocus = null;
  const readers = new Map();
  const mobile = window.matchMedia("(max-width: 720px)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const categoryColors = ["#0071e3", "#c9252d", "#8a5a00", "#7e22ce", "#1d7f34", "#008a91", "#9e477f", "#787857", "#686f8d", "#6e6e73"];
  const format = (n) => Number(n || 0).toLocaleString();
  const escape = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
  const displayDate = (date) => {
    if (!date) return "Unknown date";
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(`${date}T12:00:00Z`));
  };
  const messageButtons = () => [...el("dashboard-messages").querySelectorAll("[data-message]")];

  function syncControls() {
    for (const [id, key] of [["dashboard-range", "days"], ["dashboard-account", "account"], ["dashboard-status", "status"], ["dashboard-action", "action"], ["dashboard-search", "q"], ["dashboard-sort", "sort"], ["dashboard-start", "start"], ["dashboard-end", "end"]]) el(id).value = filters[key];
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

  function change(updates, { focus = false, replace = false } = {}) {
    clearTimeout(searchTimer);
    searchTimer = null;
    const query = "q" in updates ? updates.q : el("dashboard-search").value.trim();
    const queryChanged = query !== filters.q;
    filters = { ...filters, ...updates, q: query, page: queryChanged ? 1 : updates.page || 1 };
    if (!focus && !("page" in updates) && !("sort" in updates)) { filters.focus_date = ""; filters.focus_category = ""; }
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
    el("dashboard-messages").setAttribute("aria-busy", "true");
    try {
      const response = await fetch(`/api/dashboard?${dashboardParams(filters)}`, { signal: controller.signal });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.error || `Request failed (${response.status})`);
      if (current !== sequence) return;
      data = result;
      if (result.messages.page !== filters.page) { filters.page = result.messages.page; writeURL(true); }
      render();
      if (selected && result.messages.items.some((m) => String(m.pk) === selected)) openReader(selected, false);
      else if (selected) { selected = null; closeMobileReader(); el("reader").innerHTML = '<p class="reader-placeholder">Select a message to read it.</p>'; }
    } catch (error) {
      if (error.name !== "AbortError" && current === sequence) showError(`${error.message}${data ? " Previous results are shown." : ""}`);
    } finally {
      if (current === sequence) { el("dashboard-loading").hidden = true; el("dashboard-stats").setAttribute("aria-busy", "false"); el("dashboard-messages").setAttribute("aria-busy", "false"); }
    }
  }

  function focusMessages(date, category) { change({ focus_date: date || "", focus_category: category || "" }, { focus: true }); }

  function render() {
    for (const [id, key] of [["dashboard-total", "total"], ["dashboard-pending", "pending_attention"], ["dashboard-completed", "completed"], ["dashboard-unclassified", "unclassified"]]) el(id).textContent = format(data.summary[key]);
    el("dashboard-retryable").textContent = `${format(data.summary.retryable)} retryable`;
    const h = data.history || {};
    const notes = [`${displayDate(data.filters.start)} – ${displayDate(data.filters.end)} · ${filters.tz}`];
    if (h.collection_start_date) notes.push(`Collection started ${displayDate(h.collection_start_date)}`);
    if (h.earliest_received_date) notes.push(`Earliest stored message ${displayDate(h.earliest_received_date)}`);
    if (h.retention_days) notes.push(`${h.retention_days}-day retention`);
    if (data.daily.some((day) => day.partial)) notes.push("Days marked * have partial collection history; an empty day may have no stored history.");
    el("dashboard-history").textContent = notes.join(" · ");
    renderChart(); renderCategoryBars(); renderTable(); renderMessages();
  }

  function renderChart() {
    const fallback = el("dashboard-chart-fallback");
    if (typeof Chart !== "function") { fallback.hidden = false; el("dashboard-chart").parentElement.hidden = true; el("dashboard-table-details").open = true; return; }
    const palette = getComputedStyle(document.documentElement);
    const text = palette.getPropertyValue("--text-2").trim();
    const datasets = data.categories.map((category, index) => ({ label: category.label, category: category.name, data: data.daily.map((day) => day.counts[category.name] || 0), backgroundColor: categoryColors[index % categoryColors.length], borderWidth: 0 }));
    try {
      chart?.destroy();
      chart = new Chart(el("dashboard-chart"), {
        type: "bar", data: { labels: data.daily.map((day) => day.date), datasets },
        options: { responsive: true, maintainAspectRatio: false, animation: reducedMotion.matches ? false : { duration: 180 },
          interaction: { mode: "nearest", intersect: true },
          scales: { x: { stacked: true, grid: { display: false }, ticks: { color: text, maxTicksLimit: 10, callback(value) { return displayDate(this.getLabelForValue(value)).replace(/, \d{4}$/, ""); } } }, y: { stacked: true, beginAtZero: true, ticks: { color: text, precision: 0 }, grid: { color: palette.getPropertyValue("--separator").trim() } } },
          plugins: { legend: { display: false }, tooltip: { callbacks: { title(items) { const day = data.daily[items[0].dataIndex]; return `${displayDate(day.date)}${day.partial ? " · Partial history" : ""}`; } } } },
          onClick(event, elements) { if (!elements.length) return; const hit = elements[0]; focusMessages(data.daily[hit.index].date, datasets[hit.datasetIndex].category); },
          onHover(event, elements) { event.native.target.style.cursor = elements.length ? "pointer" : "default"; },
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
    const table = el("dashboard-daily-table");
    table.querySelector("thead").innerHTML = `<tr><th scope="col">Day</th>${data.categories.map((c) => `<th scope="col">${escape(c.label)}</th>`).join("")}<th scope="col">Total</th></tr>`;
    table.querySelector("tbody").innerHTML = data.daily.map((day) => `<tr><th scope="row">${escape(displayDate(day.date))}${day.partial ? '<span title="Partial collection history"> *</span>' : ""}</th>${data.categories.map((c) => `<td><button type="button" data-focus-date="${day.date}" data-focus-category="${escape(c.name)}" aria-label="Explore ${escape(c.label)} on ${escape(displayDate(day.date))}: ${format(day.counts[c.name])} messages">${format(day.counts[c.name])}</button></td>`).join("")}<td><button type="button" data-focus-date="${day.date}" aria-label="Explore all ${format(day.total)} messages on ${escape(displayDate(day.date))}">${format(day.total)}</button></td></tr>`).join("");
  }

  function renderMessages() {
    const messages = data.messages;
    const list = el("dashboard-messages");
    const scroll = list.scrollTop;
    const focused = document.activeElement?.dataset.message;
    const visible = messages.items.length;
    const start = visible ? (messages.page - 1) * messages.page_size + 1 : 0;
    const end = visible ? start + visible - 1 : 0;
    el("dashboard-message-count").textContent = `${format(messages.total)} messages · Showing ${format(start)}–${format(end)}`;
    const focus = [filters.focus_date && displayDate(filters.focus_date), filters.focus_category && (data.categories.find((c) => c.name === filters.focus_category)?.label || filters.focus_category)].filter(Boolean);
    el("dashboard-focus").hidden = !focus.length;
    el("dashboard-focus").querySelector("span").textContent = `Explorer selection: ${focus.join(" · ")}`;
    list.innerHTML = messages.items.map((m) => `<button type="button" class="analytics-message" data-message="${m.pk}" aria-pressed="${String(m.pk) === selected}"><span class="analytics-message-meta"><span>${escape(m.from_name || m.from_addr || "Unknown sender")}</span><span>${escape(displayDate(m.date))}${m.date_estimated ? " · Estimated" : ""}</span></span><span class="analytics-message-title">${escape(m.subject || "(No subject)")}</span>${m.company || m.role ? `<span class="analytics-message-company">${escape([m.company, m.role].filter(Boolean).join(" · "))}</span>` : ""}<span class="analytics-message-category">${escape(m.category_label)}</span><span class="analytics-message-category">${escape(m.account_label)}</span>${m.handled_at ? '<span class="analytics-message-category">Done</span>' : ""}</button>`).join("") || '<p class="analytics-empty">No messages match these filters. Adjust the filters or clear the chart selection.</p>';
    list.scrollTop = scroll;
    if (focused) (messageButtons().find((b) => b.dataset.message === focused) || messageButtons()[0] || list).focus({ preventScroll: true });
    el("dashboard-prev").disabled = messages.page <= 1;
    el("dashboard-next").disabled = messages.page >= messages.total_pages;
    el("dashboard-page").textContent = `Page ${messages.page} of ${Math.max(1, messages.total_pages)}`;
  }

  function openMobileReader() {
    if (!mobile.matches || el("dashboard-reader-panel").classList.contains("is-open")) return;
    mobileScroll = window.scrollY; returnFocus = document.activeElement;
    el("dashboard-reader-panel").classList.add("is-open");
    el("dashboard-reader-panel").setAttribute("role", "dialog");
    el("dashboard-reader-panel").setAttribute("aria-modal", "true");
    document.querySelector("nav.nav").inert = true;
    for (const child of el("analytics").children) {
      if (child.contains(el("dashboard-reader-panel"))) continue;
      child.inert = true;
    }
    el("dashboard-list-panel").inert = true;
    el("dashboard-reader-panel").closest(".analytics-explorer").querySelector(".analytics-explorer-head").inert = true;
    el("dashboard-focus").inert = true;
    document.body.style.overflow = "hidden";
    el("dashboard-reader-back").focus();
  }

  function closeMobileReader() {
    const panel = el("dashboard-reader-panel");
    if (!panel.classList.contains("is-open")) return;
    panel.classList.remove("is-open"); panel.removeAttribute("role"); panel.removeAttribute("aria-modal");
    document.querySelector("nav.nav").inert = false;
    for (const child of el("analytics").children) child.inert = false;
    el("dashboard-list-panel").inert = false; el("dashboard-focus").inert = false;
    panel.closest(".analytics-explorer").querySelector(".analytics-explorer-head").inert = false;
    document.body.style.overflow = "";
    window.scrollTo({ top: mobileScroll, behavior: "instant" });
    const target = messageButtons().find((b) => b.dataset.message === selected) || (returnFocus?.isConnected && returnFocus) || messageButtons()[0] || el("dashboard-messages");
    target.focus({ preventScroll: true });
  }

  async function openReader(pk, userInitiated = true) {
    pk = String(pk); selected = pk;
    messageButtons().forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.message === pk)));
    if (userInitiated) openMobileReader();
    readerController?.abort(); readerController = new AbortController();
    const current = ++readerSequence;
    const reader = el("reader"), item = data?.messages.items.find((m) => String(m.pk) === pk);
    reader.className = `reader tier ${item?.tier || "info"}`;
    reader.setAttribute("aria-busy", "true");
    reader.classList.add("is-loading");
    try {
      let html = readers.get(pk);
      if (!html) {
        const response = await fetch(`/api/messages/${encodeURIComponent(pk)}/reader`, { signal: readerController.signal });
        if (!response.ok) throw new Error(`Could not load message (${response.status})`);
        html = await response.text();
      }
      if (current !== readerSequence || selected !== pk) return;
      readers.set(pk, html);
      const scroll = reader.scrollTop;
      reader.innerHTML = html;
      reader.scrollTop = userInitiated ? 0 : scroll;
    } catch (error) {
      if (error.name !== "AbortError" && current === readerSequence) reader.innerHTML = `<p class="analytics-empty">${escape(error.message)}<br><button type="button" class="btn sm" data-reader-retry>Try again</button></p>`;
    } finally { if (current === readerSequence) { reader.classList.remove("is-loading"); reader.setAttribute("aria-busy", "false"); } }
  }

  function invalidateReaders(pk) {
    readerController?.abort();
    ++readerSequence;
    if (pk == null) readers.clear();
    else readers.delete(String(pk));
    el("reader").classList.remove("is-loading");
    el("reader").setAttribute("aria-busy", "false");
  }

  window.setDone = async (pk, done, btn) => {
    await withBusy(btn, async () => {
      try { const result = await api(`/api/messages/${pk}/handled?done=${done}`); invalidateReaders(pk); await load(); toast(result.message || (done ? "Marked done." : "Restored.")); }
      catch (error) { toast(error.message, false); }
    });
  };
  window.reclassifyOne = async (pk, btn) => {
    await withBusy(btn, async () => {
      try { await api("/api/reclassify", { pks: [pk] }); invalidateReaders(pk); toast("Asking the model again…"); el("bar").hidden = false; pollStatus(); }
      catch (error) { toast(error.message, false); }
    });
  };

  el("dashboard-filters").addEventListener("submit", (event) => { event.preventDefault(); change({ q: el("dashboard-search").value.trim() }); });
  for (const [id, key] of [["dashboard-account", "account"], ["dashboard-status", "status"], ["dashboard-action", "action"], ["dashboard-sort", "sort"]]) el(id).addEventListener("change", () => change({ [key]: el(id).value }));
  el("dashboard-range").addEventListener("change", () => {
    if (el("dashboard-range").value === "custom") {
      clearTimeout(searchTimer); searchTimer = null;
      const query = el("dashboard-search").value.trim();
      if (query !== filters.q) change({ q: query });
      el("dashboard-range").value = "custom";
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
  el("dashboard-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => change({ q: el("dashboard-search").value.trim() }), 250); });
  document.querySelectorAll('#dashboard-filters [name="category"]').forEach((box) => box.addEventListener("change", () => change({ categories: [...document.querySelectorAll('#dashboard-filters [name="category"]:checked')].map((b) => b.value) })));
  el("dashboard-all-categories").addEventListener("click", () => change({ categories: [] }));
  el("dashboard-reset").addEventListener("click", () => { clearTimeout(searchTimer); searchTimer = null; filters = dashboardReadFilters("", timezone); syncControls(); writeURL(); load(); });
  el("dashboard-clear-focus").addEventListener("click", () => focusMessages("", ""));
  el("dashboard-prev").addEventListener("click", () => { el("dashboard-messages").scrollTop = 0; change({ page: filters.page - 1 }, { focus: true }); });
  el("dashboard-next").addEventListener("click", () => { el("dashboard-messages").scrollTop = 0; change({ page: filters.page + 1 }, { focus: true }); });
  for (const id of ["dashboard-refresh", "dashboard-retry"]) el(id).addEventListener("click", () => { invalidateReaders(); load(); });
  el("dashboard-reader-back").addEventListener("click", closeMobileReader);
  el("analytics").addEventListener("click", (event) => {
    const message = event.target.closest("[data-message]");
    if (message) openReader(message.dataset.message);
    const category = event.target.closest("[data-category-filter]");
    if (category) change({ categories: [category.dataset.categoryFilter] });
    const day = event.target.closest("[data-focus-date]");
    if (day) focusMessages(day.dataset.focusDate, day.dataset.focusCategory);
    if (event.target.closest("[data-reader-retry]")) openReader(selected, false);
  });
  el("dashboard-messages").addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    const buttons = messageButtons(), index = buttons.indexOf(document.activeElement);
    const next = buttons[Math.max(0, Math.min(buttons.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))];
    if (next) { event.preventDefault(); next.focus(); openReader(next.dataset.message, false); }
  });
  document.addEventListener("keydown", (event) => {
    if (!el("dashboard-reader-panel").classList.contains("is-open")) return;
    if (event.key === "Escape") { event.preventDefault(); closeMobileReader(); }
    if (event.key !== "Tab") return;
    const controls = [...el("dashboard-reader-panel").querySelectorAll('button:not([disabled]), a[href], [tabindex="0"]')].filter((item) => item.offsetParent !== null);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  window.addEventListener("popstate", () => { clearTimeout(searchTimer); filters = dashboardReadFilters(location.search, timezone); syncControls(); load(); });
  window.addEventListener("mailcheck:job-finished", (event) => { event.preventDefault(); invalidateReaders(); load(); });
  mobile.addEventListener("change", () => { if (!mobile.matches) closeMobileReader(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (data) renderChart(); });
  syncControls(); writeURL(true); load();
});
