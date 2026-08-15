/* Console interactions. Every mutating action goes through api() so errors
   surface as a toast instead of a blank page or a silent failure. */

async function api(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

function toast(message, ok = true, ms = 4200) {
  const host = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${ok ? "ok" : "bad"}`;
  el.innerHTML = `<span class="mark" aria-hidden="true">${ok ? "✓" : "!"}</span><span class="msg"></span>`;
  el.querySelector(".msg").textContent = message;
  host.appendChild(el);

  // Failures interrupt; successes wait their turn in the polite region.
  if (!ok) {
    const alerts = document.getElementById("alerts");
    if (alerts) alerts.textContent = message;
  }
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, ok ? ms : Math.max(ms, 7000));
}

/** Run an async action with the button showing a spinner and staying disabled. */
async function withBusy(btn, fn) {
  if (!btn) return fn();
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>${btn.dataset.busy || ""}`;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.innerHTML = label;
  }
}

/* ------------------------------------------------------------ check + status */

let polling = false;

async function runCheck(opts = {}) {
  const bar = document.getElementById("bar");
  try {
    await api("/api/check", opts);
    bar.hidden = false;
    document.querySelectorAll("[data-check]").forEach((b) => (b.disabled = true));
    pollStatus();
  } catch (e) {
    toast(e.message, false);
  }
}

async function pollStatus() {
  if (polling) return;
  polling = true;
  const bar = document.getElementById("bar");
  const status = document.getElementById("status-line");

  while (true) {
    let s;
    try {
      s = await (await fetch("/api/status")).json();
    } catch (e) {
      break;
    }
    if (status && s.message) status.textContent = s.message;
    syncSchedule(s.auto, s.next_in);
    if (!s.running) {
      bar.hidden = true;
      document.querySelectorAll("[data-check]").forEach((b) => (b.disabled = false));
      if (s.at) {
        // Announce before reloading, or the pending popups are lost with the page.
        await flushNotifications();
        toast(s.detail ? `${s.message}\n${s.detail}` : s.message, s.ok);
        setTimeout(() => location.reload(), s.ok ? 700 : 2600);
      }
      break;
    }
    bar.hidden = false;
    await new Promise((r) => setTimeout(r, 900));
  }
  polling = false;
}

/* --------------------------------------------------- browser notifications */

const NOTIFY_KEY = "mailcheck.notify";

function notifySupported() {
  return typeof Notification !== "undefined";
}

/** On only when the user opted in AND the browser still grants permission. */
function notifyEnabled() {
  return (
    notifySupported() &&
    Notification.permission === "granted" &&
    localStorage.getItem(NOTIFY_KEY) === "on"
  );
}

async function enableNotifications() {
  if (!notifySupported()) {
    toast("This browser does not support notifications.", false);
    return false;
  }
  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();

  if (permission !== "granted") {
    localStorage.removeItem(NOTIFY_KEY);
    toast(
      permission === "denied"
        ? "Notifications are blocked for this site. Allow them in your browser's site settings, then try again."
        : "Notification permission was not granted.",
      false
    );
    return false;
  }
  localStorage.setItem(NOTIFY_KEY, "on");
  new Notification("mail-check", {
    body: "You'll be notified when an interview invite, assessment, or offer arrives.",
    tag: "mailcheck-enabled",
  });
  return true;
}

function disableNotifications() {
  localStorage.removeItem(NOTIFY_KEY);
}

/** Ask the server what has not been announced yet, show it, then ack only
    what actually got shown. The server never marks anything on our behalf —
    if it did, a denied permission or a constructor error between the fetch
    and the popup would lose that alert permanently, since it would never be
    offered again. */
async function flushNotifications() {
  if (!notifyEnabled()) return;
  let items;
  try {
    ({ items } = await (await fetch("/api/notifications")).json());
  } catch (e) {
    return;
  }
  if (!items || !items.length) return;

  const shownPks = [];

  // A burst of individual popups is worse than one summary.
  const shown = items.slice(0, 3);
  for (const item of shown) {
    const parts = [item.summary];
    if (item.deadline) parts.unshift(item.deadline);
    try {
      const n = new Notification(`${item.title} — ${item.who}`, {
        body: parts.join(" · "),
        tag: `mailcheck-${item.pk}`,
        requireInteraction: false,
      });
      n.onclick = () => {
        window.focus();
        window.open(item.url, "_blank", "noopener");
        n.close();
      };
      shownPks.push(item.pk);
    } catch (e) {
      // Not shown — leave it unacked so it is offered again next time.
    }
  }
  if (items.length > shown.length) {
    const rest = items.length - shown.length;
    try {
      const n = new Notification(`${rest} more urgent email${rest === 1 ? "" : "s"}`, {
        body: "Open mail-check to see the full queue.",
        tag: "mailcheck-more",
      });
      n.onclick = () => {
        window.focus();
        location.href = "/?view=queue";
        n.close();
      };
    } catch (e) { /* the individual popups above already tried */ }
  }

  if (shownPks.length) {
    try {
      await fetch("/api/notifications/ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(shownPks),
      });
    } catch (e) { /* next flush will just show them again */ }
  }
}

/* ------------------------------------------------------- next-check countdown */

/* The server sends seconds remaining, never a timestamp, so a browser clock that
   disagrees with the server's can't skew the countdown. We tick locally between
   polls and re-sync on every status response. */
let deadline = null;   // performance-clock ms when the next check is due
let autoOn = false;

function syncSchedule(auto, nextIn) {
  autoOn = !!auto;
  deadline = nextIn === null || nextIn === undefined ? null : performance.now() + nextIn * 1000;
  renderCountdown();
}

function renderCountdown() {
  const el = document.getElementById("next-check");
  if (!el) return;
  if (!autoOn || deadline === null) { el.hidden = true; return; }

  const left = Math.max(0, Math.round((deadline - performance.now()) / 1000));
  const m = Math.floor(left / 60);
  const s = left % 60;
  const clock = m >= 60
    ? `${Math.floor(m / 60)}h ${m % 60}m`
    : (m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`);

  el.hidden = false;
  el.classList.toggle("soon", left <= 60);
  el.textContent = left === 0 ? "Checking soon…" : `Next check in ${clock}`;

  // Once it reaches zero the server fires within a couple of seconds; poll so
  // the UI picks the run up rather than sitting on a stale "0s".
  if (left === 0 && !polling) setTimeout(pollStatus, 1200);
}

setInterval(renderCountdown, 1000);

async function toggleAuto(enabled) {
  try {
    const r = await api(`/api/autocheck?enabled=${enabled}`);
    toast(r.message);
    const s = await (await fetch("/api/status")).json();
    syncSchedule(s.auto, s.next_in);
    return true;
  } catch (e) {
    toast(e.message, false);
    return false;
  }
}

/* -------------------------------------------------------------------- sheets */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([type=hidden]), select, textarea, ' +
  'summary, [tabindex]:not([tabindex="-1"])';

let lastFocused = null;

/** Make everything outside the given scrim's own dialog non-interactive and
    invisible to assistive tech, so background content cannot be reached while
    a dialog is open — without disabling the dialog itself, which lives inside
    `<main>` as a sibling of the page's other content, not outside it. */
function setBackgroundInert(activeScrim, on) {
  const nav = document.querySelector("nav.nav");
  if (nav) nav.inert = on;
  const main = document.getElementById("main");
  if (!main) return;
  for (const child of main.children) {
    child.inert = on && child !== activeScrim;
  }
}

function openSheet(id) {
  const el = document.getElementById(id);
  if (!el) return;
  lastFocused = document.activeElement;   // restored on close
  el.hidden = false;
  setBackgroundInert(el, true);
  const first = el.querySelector("input:not([type=hidden]), select, button");
  if (first) setTimeout(() => first.focus(), 60);
}

function closeSheet(id) {
  const el = document.getElementById(id);
  if (!el || el.hidden) return;
  el.hidden = true;
  setBackgroundInert(el, false);
  if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
  lastFocused = null;
}

/** Dismiss via Escape, clicking outside, or a plain [data-close] button.
    If the dialog registered a data-on-close hook (e.g. to cancel pending work
    rather than just hide it), that hook runs instead — and it is expected to
    call closeSheet() itself once it has done its own cleanup, so this never
    calls closeSheet() when a hook exists (that would re-enter it). */
function dismissSheet(id) {
  const el = document.getElementById(id);
  if (!el || el.hidden) return;
  const hookName = el.dataset.onClose;
  const hook = hookName && window[hookName];
  if (typeof hook === "function") {
    hook();
  } else {
    closeSheet(id);
  }
}

function closeAllSheets() {
  document.querySelectorAll(".scrim:not([hidden])").forEach((s) => dismissSheet(s.id));
}

document.addEventListener("click", (e) => {
  if (e.target.classList.contains("scrim")) dismissSheet(e.target.id);
  const opener = e.target.closest("[data-open]");
  if (opener) openSheet(opener.dataset.open);
  const closer = e.target.closest("[data-close]");
  if (closer) dismissSheet(closer.dataset.close);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeAllSheets(); return; }
  if (e.key !== "Tab") return;

  // Trap Tab inside an open dialog so focus cannot wander behind the scrim.
  const open = document.querySelector(".scrim:not([hidden]) .sheet");
  if (!open) return;
  const items = [...open.querySelectorAll(FOCUSABLE)].filter(
    (el) => el.offsetParent !== null || el === document.activeElement
  );
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  // Seeded server-side so the countdown is correct on first paint.
  if (window.MAILCHECK) syncSchedule(MAILCHECK.auto, MAILCHECK.nextIn);

  const pill = document.getElementById("next-check");
  if (pill) {
    pill.addEventListener("click", async () => {
      if (await toggleAuto(false)) renderCountdown();
    });
  }

  // A check may already be running from another tab or from `watch`.
  fetch("/api/status")
    .then((r) => r.json())
    .then((s) => {
      syncSchedule(s.auto, s.next_in);
      if (s.running) pollStatus();
    })
    .catch(() => {});

  // Idle resync: picks up a schedule changed in another tab, and — importantly —
  // a check the scheduler completed while this tab was idle or backgrounded,
  // which pollStatus would otherwise never see.
  let lastAt = null;
  fetch("/api/status").then((r) => r.json()).then((s) => { lastAt = s.at; }).catch(() => {});

  setInterval(() => {
    if (polling) return;
    fetch("/api/status")
      .then((r) => r.json())
      .then((s) => {
        syncSchedule(s.auto, s.next_in);
        if (s.running) { pollStatus(); return; }
        if (s.at && s.at !== lastAt) {
          lastAt = s.at;               // a run finished behind our back
          flushNotifications();
        }
      })
      .catch(() => {});
  }, 30000);

  document.querySelectorAll("[data-check]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const form = document.getElementById("check-options");
      const opts = {};
      if (form) {
        const a = form.querySelector("[name=account]");
        const d = form.querySelector("[name=since_days]");
        const n = form.querySelector("[name=no_cache]");
        if (a && a.value) opts.account = a.value;
        if (d && d.value) opts.since_days = parseInt(d.value, 10);
        if (n && n.checked) opts.no_cache = true;
      }
      runCheck(opts);
    });
  });
});
