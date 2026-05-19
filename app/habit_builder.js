(function () {
  // Listen for theme messages from the parent shell (postMessage from
  // day_scheduler.html). Same-origin only.
  window.addEventListener("message", function (ev) {
    if (ev.origin !== window.location.origin) return;
    const d = ev.data;
    if (!d || d.source !== "scheduler-shell" || d.type !== "theme") return;
    const t = d.theme === "dark" ? "dark" : "light";
    if (document.documentElement.dataset.theme !== t) {
      document.documentElement.dataset.theme = t;
    }
  });

  const LEGACY_STORAGE_KEY = "scheduler-habit-builder";
  /** Synchronous JSON mirror of the same payload as IndexedDB — updated on every persist and on tab close. */
  const LOCAL_BACKUP_KEY = "scheduler-habit-builder-backup";
  const DB_NAME = "scheduler-habit-builder-db";
  const DB_VERSION = 1;
  const STORE_NAME = "state";
  const TOTAL_POINTS = 4095; // 28 + sum(8..90)
  const PHASE2_MAX_POINTS = 4067; // sum(8..90)
  /** Leg 8 … Leg 90 on-time mandatory rests (calendar Phase 2). */
  const PHASE2_NOMINAL_REST_DAY_COUNT = 83;
  const PHASE1_WEEKS_WORKNIGHT = 5;
  const PHASE1_MAX_POINTS_WORKNIGHT = 15; // 1+2+3+4+5
  const TOTAL_POINTS_WORKNIGHT = PHASE1_MAX_POINTS_WORKNIGHT + PHASE2_MAX_POINTS;

  /** Show enough fractional digits that +1 point (~100/4095 %) is visible. */
  function formatProgressPct(pct) {
    const p = Math.min(100, Math.max(0, pct));
    if (p >= 100) return "100";
    return p.toFixed(4);
  }

  /** @type {Promise<IDBDatabase> | null} */
  let dbPromise = null;

  function openDb() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onerror = () => reject(req.error);
        req.onupgradeneeded = (e) => {
          const db = e.target.result;
          if (!db.objectStoreNames.contains(STORE_NAME)) {
            db.createObjectStore(STORE_NAME, { keyPath: "id" });
          }
        };
        req.onsuccess = () => resolve(req.result);
      });
    }
    return dbPromise;
  }

  /** @returns {Promise<{ id: string, habits: unknown[], selectedId: string | null } | null>} */
  async function readStateRow() {
    const db = await openDb();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const q = tx.objectStore(STORE_NAME).get("default");
      q.onsuccess = () => resolve(q.result ?? null);
      q.onerror = () => reject(q.error);
    });
  }

  function normalizeHabit(raw) {
    if (!raw || typeof raw !== "object") return null;
    const id = typeof raw.id === "string" ? raw.id : null;
    const title = typeof raw.title === "string" ? raw.title : "";
    const start = typeof raw.start === "string" ? raw.start : todayISO();
    const days =
      raw.days && typeof raw.days === "object" && !Array.isArray(raw.days) ? { ...raw.days } : {};
    for (const k of Object.keys(days)) {
      if (!days[k]) delete days[k];
    }
    let habit_type =
      typeof raw.habit_type === "string" && raw.habit_type === "worknight" ? "worknight" : "default";
    const cheat_days_raw =
      raw.cheat_days && typeof raw.cheat_days === "object" && !Array.isArray(raw.cheat_days)
        ? { ...raw.cheat_days }
        : {};
    const cheat_days = {};
    for (const k of Object.keys(cheat_days_raw)) {
      if (cheat_days_raw[k]) cheat_days[k] = true;
    }
    if (!id) return null;
    return { id, title, start, days, habit_type, cheat_days };
  }

  function migrateFromLocalStorage() {
    try {
      const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (!Array.isArray(data)) return null;
      const habitsNorm = [];
      for (const item of data) {
        const h = normalizeHabit(item);
        if (h) habitsNorm.push(h);
      }
      return {
        id: "default",
        habits: habitsNorm,
        selectedId: habitsNorm[0]?.id ?? null,
        migratedFrom: "localStorage",
      };
    } catch {
      return null;
    }
  }

  async function writeStateRow(row) {
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.oncomplete = () => resolve(undefined);
      tx.onerror = () => reject(tx.error);
      tx.objectStore(STORE_NAME).put({
        ...row,
        updatedAt: row.updatedAt ?? Date.now(),
      });
    });
  }

  function setStorageStatus(ok, msg) {
    const el = document.getElementById("storage-status");
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle("muted", ok);
  }

  /** @type {{ id: string, title: string, start: string, days: Record<string, boolean>, habit_type: string, cheat_days: Record<string, boolean> }[]} */
  let habits = [];
  /** @type {string | null} */
  let habitTypePopoverHabitId = null;
  /** @type {(() => void) | null} */
  let habitTypePopoverTeardown = null;
  /** @type {string | null} */
  let selectedId = null;
  /** When true, persistence uses ``PUT /api/habits`` (same origin as the day-scheduler server). */
  let useHabitsRestApi = false;
  let viewYear = new Date().getFullYear();
  let viewMonth = new Date().getMonth();

  function ingestStorageRow(row) {
    const list = [];
    for (const item of row.habits) {
      const h = normalizeHabit(item);
      if (h) list.push(h);
    }
    habits = list;
    /** @type {string | null} */
    let sid;
    if (row != null && Object.prototype.hasOwnProperty.call(row, "selectedId")) {
      sid = row.selectedId === null ? null : typeof row.selectedId === "string" ? row.selectedId : null;
    } else {
      sid = habits[0]?.id ?? null;
    }
    if (sid !== null && !habits.some((x) => x.id === sid)) sid = habits[0]?.id ?? null;
    selectedId = sid;
  }

  function buildStatePayload() {
    return {
      id: "default",
      habits: habits.map((h) => ({
        id: h.id,
        title: h.title,
        start: h.start,
        days: { ...(h.days || {}) },
        habit_type: h.habit_type === "worknight" ? "worknight" : "default",
        cheat_days: { ...(h.cheat_days || {}) },
      })),
      selectedId,
      updatedAt: Date.now(),
    };
  }

  function mirrorLocalStorageBackup() {
    try {
      localStorage.setItem(LOCAL_BACKUP_KEY, JSON.stringify(buildStatePayload()));
    } catch (e) {
      console.warn("habit-builder: localStorage mirror failed", e);
    }
  }

  function tryRestoreFromLocalBackup() {
    try {
      const raw = localStorage.getItem(LOCAL_BACKUP_KEY);
      if (!raw) return false;
      const row = JSON.parse(raw);
      if (!row || !Array.isArray(row.habits)) return false;
      ingestStorageRow(row);
      return true;
    } catch {
      return false;
    }
  }

  function notifyParentDaySchedulerShell() {
    try {
      if (window.parent === window) return;
      window.parent.postMessage(
        { source: "scheduler-habit-builder", type: "habits-updated" },
        window.location.origin,
      );
    } catch (_) {}
  }

  async function persistState() {
    const payload = buildStatePayload();
    mirrorLocalStorageBackup();
    if (useHabitsRestApi) {
      try {
        const resp = await fetch("/api/habits", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!resp.ok) throw new Error("PUT /api/habits failed: " + resp.status);
        setStorageStatus(
          true,
          habits.length === 0
            ? "No habits yet — saved to SQLite."
            : `${habits.length} habit(s) · SQLite.`,
        );
      } catch (e) {
        console.error("habit-builder: SQLite API save failed", e);
        setStorageStatus(false, "Could not save to server — check day_scheduler UI is running.");
      }
      notifyParentDaySchedulerShell();
      return;
    }
    await writeStateRow(payload);
    setStorageStatus(
      true,
      habits.length === 0
        ? "No habits yet — changes save to IndexedDB + localStorage backup on every action."
        : `${habits.length} habit(s) · auto-saved to IndexedDB and localStorage on each change.`,
    );
    notifyParentDaySchedulerShell();
  }

  function save() {
    void persistState().catch((e) => {
      console.error("habit-builder: persist failed (IndexedDB)", e);
      try {
        mirrorLocalStorageBackup();
      } catch (_) {}
    });
  }

  async function loadFromIndexedDbOnly() {
    let row = null;
    try {
      row = await readStateRow();
    } catch (e) {
      console.warn("habit-builder: IndexedDB read failed", e);
    }

    if (row != null && Array.isArray(row.habits)) {
      ingestStorageRow(row);
      mirrorLocalStorageBackup();
      return;
    }

    if (tryRestoreFromLocalBackup()) {
      console.warn("habit-builder: restored from localStorage backup (IndexedDB missing or empty)");
      try {
        await writeStateRow(buildStatePayload());
      } catch (e) {
        console.error("habit-builder: could not sync restored state to IndexedDB", e);
      }
      return;
    }

    const migrated = migrateFromLocalStorage();
    if (migrated && migrated.habits.length > 0) {
      habits = migrated.habits;
      selectedId = migrated.selectedId;
      await writeStateRow(buildStatePayload());
      mirrorLocalStorageBackup();
      try {
        localStorage.removeItem(LEGACY_STORAGE_KEY);
      } catch (_) {}
    }
  }

  async function loadInitialState() {
    try {
      const r = await fetch("/api/habits", { cache: "no-store" });
      if (r.ok) {
        useHabitsRestApi = true;
        const snap = await r.json();
        if (snap && Array.isArray(snap.habits) && snap.habits.length) {
          ingestStorageRow(snap);
          mirrorLocalStorageBackup();
          setStorageStatus(
            true,
            `${habits.length} habit(s) · SQLite (PUT /api/habits on each change).`,
          );
          return;
        }
        await loadFromIndexedDbOnly();
        const payload = buildStatePayload();
        if (payload.habits.length > 0) {
          await fetch("/api/habits", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          }).catch(() => {});
        }
        setStorageStatus(
          true,
          habits.length === 0
            ? "No habits yet — SQLite via day-scheduler server."
            : `${habits.length} habit(s) · migrated to SQLite.`,
        );
        return;
      }
    } catch (_) {
      /* same-origin API unavailable */
    }
    useHabitsRestApi = false;
    await loadFromIndexedDbOnly();
  }

  function flushBackupSync() {
    try {
      mirrorLocalStorageBackup();
    } catch (_) {}
  }

  function todayISO() {
    const t = new Date();
    return ymd(t.getFullYear(), t.getMonth() + 1, t.getDate());
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function ymd(y, m, d) {
    return `${y}-${pad(m)}-${pad(d)}`;
  }

  function parseISODate(s) {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function addDaysISO(iso, delta) {
    const dt = parseISODate(iso);
    dt.setDate(dt.getDate() + delta);
    return ymd(dt.getFullYear(), dt.getMonth() + 1, dt.getDate());
  }

  function daysDiff(isoA, isoB) {
    const a = parseISODate(isoA).getTime();
    const b = parseISODate(isoB).getTime();
    return Math.round((b - a) / 86400000);
  }

  /** Drop logged days that fall before the habit’s current program start (e.g. after correcting start). */
  function stripMarksBeforeStart(h) {
    if (!h.days) return;
    for (const k of Object.keys(h.days)) {
      if (daysDiff(h.start, k) < 0) delete h.days[k];
    }
    if (h.cheat_days) {
      for (const k of Object.keys(h.cheat_days)) {
        if (daysDiff(h.start, k) < 0) delete h.cheat_days[k];
      }
    }
  }

  function isWorknight(h) {
    return h.habit_type === "worknight";
  }

  function cheatDatesAsSet(h) {
    const s = new Set();
    const cd = h.cheat_days || {};
    for (const k of Object.keys(cd)) {
      if (cd[k]) s.add(k);
    }
    return s;
  }

  /** Cheats whose calendar date falls in [anchorISO − 29 days, anchorISO] (inclusive). */
  function cheatCountRolling30Ending(h, anchorISO) {
    const cd = h.cheat_days || {};
    let n = 0;
    for (const k of Object.keys(cd)) {
      if (!cd[k]) continue;
      const dd = daysDiff(k, anchorISO);
      if (dd >= 0 && dd <= 29) n++;
    }
    return n;
  }

  /** Sun–Thu (local): streak nights skip Fri/Sat. */
  function isSunThruThu(iso) {
    const dow = parseISODate(iso).getDay();
    return dow <= 4;
  }

  function firstSunThruThuOnOrAfter(iso) {
    let d = iso;
    let guard = 0;
    while (!isSunThruThu(d) && guard++ < 14) {
      d = addDaysISO(d, 1);
    }
    return d;
  }

  function nextSunThruThuAfter(iso) {
    return firstSunThruThuOnOrAfter(addDaysISO(iso, 1));
  }

  function weekIndexUncapped(startISO, dateISO) {
    if (daysDiff(startISO, dateISO) < 0) return -1;
    const startSun = sundayOfWeekContaining(startISO);
    const dateSun = sundayOfWeekContaining(dateISO);
    return Math.floor(daysDiff(startSun, dateSun) / 7);
  }

  function weekHasCheat(startISO, weekIdx, cheatSet) {
    const { weekStart, weekEnd } = phase1WeekRange(startISO, weekIdx);
    for (const c of cheatSet) {
      if (daysDiff(weekStart, c) >= 0 && daysDiff(c, weekEnd) >= 0) return true;
    }
    return false;
  }

  function cheatFrozenSundaySet(h) {
    const set = new Set();
    if (!isWorknight(h)) return set;
    const cd = h.cheat_days || {};
    for (const k of Object.keys(cd)) {
      if (!cd[k]) continue;
      set.add(sundayOfWeekContaining(k));
    }
    return set;
  }

  function closeHabitTypePopover() {
    if (habitTypePopoverTeardown) {
      habitTypePopoverTeardown();
      habitTypePopoverTeardown = null;
    }
    habitTypePopoverHabitId = null;
  }

  function openHabitTypePopover(habitId, anchorEl) {
    closeHabitTypePopover();
    const h = habits.find((x) => x.id === habitId);
    if (!h) return;
    habitTypePopoverHabitId = habitId;
    const backdrop = document.createElement("div");
    backdrop.className = "habit-type-modal-backdrop";
    backdrop.setAttribute("role", "presentation");
    backdrop.tabIndex = -1;

    const wrap = document.createElement("div");
    wrap.className = "habit-type-modal-panel";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");
    wrap.setAttribute("aria-labelledby", "habit-type-modal-title");

    const head = document.createElement("div");
    head.id = "habit-type-modal-title";
    head.className = "habit-type-modal-title";
    head.textContent = "Habit settings";

    const nameLabel = document.createElement("div");
    nameLabel.className = "habit-type-modal-title";
    nameLabel.textContent = "Habit name";

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "habit-settings-name-input";
    nameInput.setAttribute("aria-label", "Habit name");
    nameInput.maxLength = 120;
    nameInput.value = h.title || "";

    const typeLabel = document.createElement("div");
    typeLabel.className = "habit-type-modal-title";
    typeLabel.textContent = "Habit type";

    const sel = document.createElement("select");
    sel.className = "habit-type-select";
    sel.setAttribute("aria-label", "Habit type");
    for (const opt of [
      { v: "default", t: "Default" },
      { v: "worknight", t: "Worknight" },
    ]) {
      const o = document.createElement("option");
      o.value = opt.v;
      o.textContent = opt.t;
      sel.appendChild(o);
    }
    sel.value = h.habit_type === "worknight" ? "worknight" : "default";
    sel.addEventListener("change", () => {
      h.habit_type = sel.value === "worknight" ? "worknight" : "default";
      save();
      closeHabitTypePopover();
      render();
    });

    const hint = document.createElement("p");
    hint.className = "habit-type-modal-hint muted";
    hint.textContent =
      "Worknight: Phase 1 is 5 calendar weeks (Sun–Thu pressure; Fri/Sat optional). Tap the same calendar day twice quickly for a cheat freeze (max 2 per rolling 30 days).";

    const actions = document.createElement("div");
    actions.className = "habit-type-modal-actions";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "habit-type-modal-close";
    closeBtn.textContent = "Done";
    closeBtn.addEventListener("click", () => {
      const nextTitle = nameInput.value.trim();
      if (nextTitle && nextTitle !== h.title) {
        h.title = nextTitle;
        save();
        render();
      }
      closeHabitTypePopover();
    });

    wrap.appendChild(head);
    wrap.appendChild(nameLabel);
    wrap.appendChild(nameInput);
    wrap.appendChild(typeLabel);
    wrap.appendChild(sel);
    wrap.appendChild(hint);
    actions.appendChild(closeBtn);
    wrap.appendChild(actions);
    backdrop.appendChild(wrap);
    document.body.appendChild(backdrop);

    const onBackdropMouseDown = (ev) => {
      if (ev.target === backdrop) closeHabitTypePopover();
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        closeHabitTypePopover();
      }
    };
    backdrop.addEventListener("mousedown", onBackdropMouseDown);
    document.addEventListener("keydown", onKey, true);

    requestAnimationFrame(() => {
      nameInput.focus();
    });

    habitTypePopoverTeardown = () => {
      backdrop.removeEventListener("mousedown", onBackdropMouseDown);
      document.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      if (anchorEl) {
        anchorEl.setAttribute("aria-expanded", "false");
      }
    };
    anchorEl.setAttribute("aria-expanded", "true");
  }

  function uuid() {
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  /** Marks → sorted ISO date strings */
  function markedDates(h) {
    return Object.keys(h.days || {}).filter((k) => h.days[k]).sort();
  }

  /**
   * Furthest date we need for phase-2 simulation: today or latest mark, whichever is later.
   * When Phase 1 is satisfied and `boundaryISO` is set, never scan **before** the first Phase-2
   * calendar day (`boundary + 1`, or first Sun–Thu night after that for worknight). Otherwise
   * strict Phase-2 points stay at 0 while the latest mark is still on the last Phase-1 day.
   */
  function maxDerivedScanISO(h, boundaryISO) {
    const t = todayISO();
    const marks = markedDates(h);
    const last = marks.length ? marks[marks.length - 1] : t;
    let base = last > t ? last : t;
    if (boundaryISO) {
      const edge = isWorknight(h)
        ? firstSunThruThuOnOrAfter(addDaysISO(boundaryISO, 1))
        : addDaysISO(boundaryISO, 1);
      if (base < edge) base = edge;
    }
    return base;
  }

  /** Sunday (local) of the calendar week that contains `iso` (week = Sun–Sat). */
  function sundayOfWeekContaining(iso) {
    const dt = parseISODate(iso);
    const dow = dt.getDay();
    dt.setDate(dt.getDate() - dow);
    return ymd(dt.getFullYear(), dt.getMonth() + 1, dt.getDate());
  }

  /**
   * Phase 1 week index 0..6: Sun–Sat weeks; week 0 is the week containing program start.
   * Only dates on/after startISO participate. Indices outside 0..6 return -1.
   */
  function weekIndexForDate(startISO, dateISO) {
    if (daysDiff(startISO, dateISO) < 0) return -1;
    const startSun = sundayOfWeekContaining(startISO);
    const dateSun = sundayOfWeekContaining(dateISO);
    const w = Math.floor(daysDiff(startSun, dateSun) / 7);
    if (w < 0 || w > 6) return -1;
    return w;
  }

  /** @returns {{ weekStart: string, weekEnd: string }} Sunday..Saturday ISO for phase-1 week index w (0..6). */
  function phase1WeekRange(startISO, w) {
    const sun0 = sundayOfWeekContaining(startISO);
    const weekStart = addDaysISO(sun0, w * 7);
    const weekEnd = addDaysISO(weekStart, 6);
    return { weekStart, weekEnd };
  }

  /** @returns {{ actual: number[], required: number[], phase1Earned: number, phase1Satisfied: boolean }} */
  function phase1Stats(startISO, marks) {
    const actual = [0, 0, 0, 0, 0, 0, 0];
    for (const iso of marks) {
      if (daysDiff(startISO, iso) < 0) continue;
      const w = weekIndexForDate(startISO, iso);
      if (w >= 0 && w < 7) actual[w]++;
    }
    let phase1Earned = 0;
    let satisfied = true;
    for (let w = 0; w < 7; w++) {
      const need = w + 1;
      phase1Earned += Math.min(actual[w], need);
      if (actual[w] < need) satisfied = false;
    }
    return { actual, phase1Earned, phase1Satisfied: satisfied };
  }

  /** Which Phase 1 week (1–7) the status line highlights for ``anchorISO`` (calendar weeks). */
  function phase1CurrentWeekUiIndex(startISO, anchorISO, actual) {
    let rawW = weekIndexForDate(startISO, anchorISO);
    let w = rawW;
    if (w < 0) w = 0;
    if (w > 6) {
      let firstShort = -1;
      for (let i = 0; i < 7; i++) {
        if (actual[i] < i + 1) {
          firstShort = i;
          break;
        }
      }
      w = firstShort >= 0 ? firstShort : 6;
    }
    return w;
  }

  /** Worknight Phase 1 week index for UI (1–5). */
  function phase1CurrentWeekUiIndexWorknight(startISO, anchorISO, actual, cheatSet) {
    let rawW = weekIndexUncapped(startISO, anchorISO);
    let w = rawW;
    if (w < 0) w = 0;
    if (w > PHASE1_WEEKS_WORKNIGHT - 1) {
      let firstShort = -1;
      for (let i = 0; i < PHASE1_WEEKS_WORKNIGHT; i++) {
        const need = i + 1;
        const ok = actual[i] >= need || weekHasCheat(startISO, i, cheatSet);
        if (!ok) {
          firstShort = i;
          break;
        }
      }
      w = firstShort >= 0 ? firstShort : PHASE1_WEEKS_WORKNIGHT - 1;
    }
    return w;
  }

  function phase1StatsWorknight(startISO, marks, cheatSet) {
    const actual = [0, 0, 0, 0, 0];
    for (const iso of marks) {
      if (daysDiff(startISO, iso) < 0) continue;
      const w = weekIndexUncapped(startISO, iso);
      if (w >= 0 && w < PHASE1_WEEKS_WORKNIGHT) actual[w]++;
    }
    let phase1Earned = 0;
    let satisfied = true;
    for (let w = 0; w < PHASE1_WEEKS_WORKNIGHT; w++) {
      const need = w + 1;
      const cheatW = weekHasCheat(startISO, w, cheatSet);
      const eff = cheatW ? Math.max(actual[w], need) : actual[w];
      phase1Earned += Math.min(eff, need);
      if (!(actual[w] >= need || cheatW)) satisfied = false;
    }
    return { actual, phase1Earned, phase1Satisfied: satisfied };
  }

  /**
   * Earliest D: max over w of the (need)-th marked date in week w (chronological within week).
   * If any week lacks enough marks, returns null.
   */
  function phase2BoundaryDateWorknight(startISO, marks) {
    const byWeek = Array.from({ length: PHASE1_WEEKS_WORKNIGHT }, () => []);
    for (const iso of marks) {
      if (daysDiff(startISO, iso) < 0) continue;
      const w = weekIndexUncapped(startISO, iso);
      if (w >= 0 && w < PHASE1_WEEKS_WORKNIGHT) byWeek[w].push(iso);
    }
    for (let w = 0; w < PHASE1_WEEKS_WORKNIGHT; w++) byWeek[w].sort();
    let maxD = null;
    for (let w = 0; w < PHASE1_WEEKS_WORKNIGHT; w++) {
      const need = w + 1;
      if (byWeek[w].length < need) return null;
      const d = byWeek[w][need - 1];
      if (maxD === null || daysDiff(maxD, d) > 0) maxD = d;
    }
    return maxD;
  }

  /**
   * Earliest D: max over w of the (need)-th marked date in week w (chronological within week).
   * If any week lacks enough marks, returns null.
   */
  function phase2BoundaryDate(startISO, marks) {
    const byWeek = Array.from({ length: 7 }, () => []);
    for (const iso of marks) {
      if (daysDiff(startISO, iso) < 0) continue;
      const w = weekIndexForDate(startISO, iso);
      if (w >= 0 && w < 7) byWeek[w].push(iso);
    }
    for (let w = 0; w < 7; w++) byWeek[w].sort();
    let maxD = null;
    for (let w = 0; w < 7; w++) {
      const need = w + 1;
      if (byWeek[w].length < need) return null;
      const d = byWeek[w][need - 1];
      if (maxD === null || daysDiff(maxD, d) > 0) maxD = d;
    }
    return maxD;
  }

  /**
   * Nominal mandatory-rest ISO dates from Phase 2 calendar day 1 (perfect adherence).
   * Length {@link PHASE2_NOMINAL_REST_DAY_COUNT}.
   */
  function nominalPhase2RestDates(phase2StartISO) {
    if (!phase2StartISO || typeof phase2StartISO !== "string") return [];
    const cur = parseISODate(phase2StartISO);
    if (Number.isNaN(cur.getTime())) return [];
    let cursor = phase2StartISO;
    const out = [];
    for (let n = 8; n <= 90; n++) {
      const rest = addDaysISO(cursor, n);
      out.push(rest);
      cursor = addDaysISO(rest, 1);
    }
    return out;
  }

  /**
   * Long-run leg UI: count marks in the current nominal leg window (gap = calendar days from leg start
   * to mandatory rest). Matches "Next Rest" nominal schedule. Uses max(today, latest Phase 2 mark) so a
   * pre-logged next-leg day rolls the counter forward while today is still the rest day.
   * @returns {{ label: string, counter: string, targetLen: number, run: number } | null}
   */
  function phase2LegProgressFromNominalRests(phase2StartISO, marksSet, todayISO) {
    if (!phase2StartISO || typeof phase2StartISO !== "string") return null;
    const rests = nominalPhase2RestDates(phase2StartISO);
    if (!rests.length) return null;
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    let anchorISO = todayISO;
    for (const iso of mark) {
      if (daysDiff(phase2StartISO, iso) >= 0 && iso > anchorISO) anchorISO = iso;
    }
    let legStart = phase2StartISO;
    for (const rest of rests) {
      if (daysDiff(anchorISO, rest) >= 0) {
        const gap = daysDiff(legStart, rest);
        if (gap < 8 || gap > 90) return null;
        let filled = 0;
        for (const iso of mark) {
          if (daysDiff(legStart, iso) >= 0 && daysDiff(iso, rest) > 0) filled++;
        }
        const label = formatLegLabel(gap);
        if (!label) return null;
        return { label, counter: `${filled}/${gap}`, targetLen: gap, run: filled };
      }
      legStart = addDaysISO(rest, 1);
    }
    const gap = 90;
    const restAfter = addDaysISO(legStart, gap);
    let filled = 0;
    for (const iso of mark) {
      if (daysDiff(legStart, iso) >= 0 && daysDiff(iso, restAfter) > 0) filled++;
    }
    const label = formatLegLabel(gap);
    if (!label) return null;
    return { label, counter: `${filled}/${gap}`, targetLen: gap, run: filled };
  }

  /**
   * @returns {{ phase2Earned: number, complete: boolean, targetL: number, run: number, needRest: boolean, violation: boolean }}
   */
  function simulatePhase2(boundaryISO, marksSet, maxScanISO) {
    const mark = new Set(marksSet);
    let day = addDaysISO(boundaryISO, 1);
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let phase2Earned = 0;
    let complete = false;
    let violation = false;

    let guard = 0;
    while (daysDiff(day, maxScanISO) >= 0 && !complete && guard < 12000) {
      guard++;
      const done = mark.has(day);
      if (needRest) {
        if (done) {
          violation = true;
          break;
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else {
        if (done) {
          run++;
          phase2Earned++;
          if (run === targetL) {
            run = 0;
            if (targetL === 90) {
              complete = true;
            } else {
              needRest = true;
            }
          }
        } else {
          run = 0;
        }
      }
      day = addDaysISO(day, 1);
    }

    return { phase2Earned, complete, targetL, run, needRest, violation };
  }

  /** Sorted ISO dates on/after first Phase 2 day (boundary + 1) that are marked. */
  function phase2MarkedDatesAsc(boundaryISO, marksSet) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    const out = [];
    for (const iso of mark) {
      if (daysDiff(firstP2, iso) >= 0) out.push(iso);
    }
    out.sort();
    return out;
  }

  /**
   * Maximal contiguous runs of calendar days; each run is { start, end, length }.
   * @param {string[]} sortedAsc
   */
  function contiguousRunsFromSorted(sortedAsc) {
    if (!sortedAsc.length) return [];
    const runs = [];
    let runStart = sortedAsc[0];
    let prev = sortedAsc[0];
    let len = 1;
    for (let i = 1; i < sortedAsc.length; i++) {
      const iso = sortedAsc[i];
      if (daysDiff(prev, iso) === 1) {
        len += 1;
      } else {
        runs.push({ start: runStart, end: prev, length: len });
        runStart = iso;
        len = 1;
      }
      prev = iso;
    }
    runs.push({ start: runStart, end: prev, length: len });
    return runs;
  }

  /** Longest run length among Phase 2 marks (for 90-day shortcut). */
  function maxPhase2RunLength(boundaryISO, marksSet) {
    const asc = phase2MarkedDatesAsc(boundaryISO, marksSet);
    const runs = contiguousRunsFromSorted(asc);
    let m = 0;
    for (const r of runs) if (r.length > m) m = r.length;
    return m;
  }

  /** Last maximal contiguous run length (greedy milestone packing). */
  function terminalPhase2RunLength(boundaryISO, marksSet) {
    const asc = phase2MarkedDatesAsc(boundaryISO, marksSet);
    const runs = contiguousRunsFromSorted(asc);
    return runs.length ? runs[runs.length - 1].length : 0;
  }

  /**
   * Greedy Phase 2 points from terminal run only + shortcut.
   * @returns {{ forgivingPhase2Pts: number, forgivingComplete: boolean }}
   */
  function simulatePhase2Forgiving(boundaryISO, marksSet) {
    const maxLen = maxPhase2RunLength(boundaryISO, marksSet);
    if (maxLen >= 90) {
      return { forgivingPhase2Pts: PHASE2_MAX_POINTS, forgivingComplete: true };
    }
    const R = terminalPhase2RunLength(boundaryISO, marksSet);
    if (R <= 0) {
      return { forgivingPhase2Pts: 0, forgivingComplete: false };
    }
    let pos = 0;
    let m = 8;
    let earned = 0;
    while (m <= 90) {
      const rem = R - pos;
      if (rem >= m) {
        pos += m;
        earned += m;
        m += 1;
      } else {
        earned += rem;
        break;
      }
    }
    const forgivingComplete = earned >= PHASE2_MAX_POINTS;
    const capped = Math.min(PHASE2_MAX_POINTS, earned);
    return { forgivingPhase2Pts: capped, forgivingComplete };
  }

  /**
   * Re-run simulation tracking rest-window for UI (forgiving Phase 2 ladder from firstP2; no Phase 1 carry-in).
   */
  function deriveRestDaySet(boundaryISO, marksSet, habitStartISO) {
    const set = new Set();
    const mark = new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    let day = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let complete = false;
    const maxScanISO = addDaysISO(todayISO(), 365 * 5);
    let guard = 0;
    while (daysDiff(day, maxScanISO) >= 0 && !complete && guard < 12000) {
      guard++;
      const done = mark.has(day);
      if (needRest) {
        set.add(day);
        if (done) break;
        needRest = false;
        if (targetL < 90) targetL++;
      } else {
        if (done) {
          run++;
          if (run === targetL) {
            run = 0;
            if (targetL === 90) complete = true;
            else needRest = true;
          }
        } else {
          run = 0;
        }
      }
      day = addDaysISO(day, 1);
    }
    return set;
  }

  /**
   * Phase-2 state at the **start** of `dayISO` (marks on that day not yet applied).
   */
  function stateAtStartOfDay(boundaryISO, marksSet, dayISO) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    if (daysDiff(dayISO, firstP2) < 0) {
      return {
        targetL: 8,
        run: 0,
        needRest: false,
        violation: false,
        beforePhase2: true,
      };
    }
    let day = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let violation = false;
    let guard = 0;
    while (daysDiff(day, dayISO) < 0 && !violation && guard < 12000) {
      guard++;
      const done = mark.has(day);
      if (needRest) {
        if (done) {
          violation = true;
          break;
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else {
        if (done) {
          run++;
          if (run === targetL) {
            run = 0;
            if (targetL === 90) {
              /** program done; no further phase-2 expectations */
            } else {
              needRest = true;
            }
          }
        } else {
          run = 0;
        }
      }
      day = addDaysISO(day, 1);
    }
    return { targetL, run, needRest, violation, beforePhase2: false };
  }

  /**
   * Same as {@link stateAtStartOfDay}, but when replaying **past** days (`day < dayISO`), a mark on a
   * mandatory **rest** slot is ignored so planner UI still shows next rest / next log after old mistakes.
   * (Strict simulation and points still use {@link stateAtStartOfDay}.)
   *
   * Phase 2 ladder **starts empty at firstP2** (L8, run 0). Phase 1 marks are **not** replayed as ladder
   * carry-in—otherwise weekly quota streaks are mistaken for partial long-run legs (e.g. showing 7/8 on
   * the first Phase 2 day and the wrong “next rest”).
   * @param {string} [habitStartISO] — unused; kept for call compatibility.
   */
  function stateAtStartOfDayForgiving(boundaryISO, marksSet, dayISO, habitStartISO) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    if (daysDiff(dayISO, firstP2) < 0) {
      return {
        targetL: 8,
        run: 0,
        needRest: false,
        violation: false,
        beforePhase2: true,
      };
    }
    let day = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let violation = false;
    let guard = 0;
    while (daysDiff(day, dayISO) < 0 && !violation && guard < 12000) {
      guard++;
      let done = mark.has(day);
      if (needRest) {
        if (done) {
          if (daysDiff(day, dayISO) < 0) {
            done = false;
          } else {
            violation = true;
            break;
          }
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else {
        if (done) {
          run++;
          if (run === targetL) {
            run = 0;
            if (targetL === 90) {
              /** complete */
            } else {
              needRest = true;
            }
          }
        } else {
          run = 0;
        }
      }
      day = addDaysISO(day, 1);
    }
    return { targetL, run, needRest, violation, beforePhase2: false };
  }

  /** Next calendar day (on/after `startFromISO`) where Phase 2 requires a rest (unchecked day). */
  function nextNeedRestStartISO(boundaryISO, marksSet, startFromISO, habitStartISO) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    let d = startFromISO;
    for (let i = 0; i < 800; i++) {
      const st = stateAtStartOfDayForgiving(boundaryISO, mark, d, habitStartISO);
      if (st.violation || st.beforePhase2) return null;
      if (st.needRest) return d;
      d = addDaysISO(d, 1);
    }
    return null;
  }

  /** Same for worknight: only considers Sun–Thu dates as eligible “nights” (matches planner UI). */
  function nextNeedRestStartISOWorknight(boundaryISO, marksSet, startFromISO, cheatFrozenSuns, habitStartISO) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const cheatFrozen =
      cheatFrozenSuns instanceof Set ? cheatFrozenSuns : new Set(cheatFrozenSuns || []);
    let d = startFromISO;
    for (let i = 0; i < 800; i++) {
      if (!isSunThruThu(d)) {
        d = addDaysISO(d, 1);
        continue;
      }
      const st = stateAtStartOfDayWorknightForgiving(boundaryISO, mark, d, cheatFrozen, habitStartISO);
      if (st.violation || st.beforePhase2) return null;
      if (st.needRest) return d;
      d = addDaysISO(d, 1);
    }
    return null;
  }

  /**
   * Next mandatory rest on/after `todayISO`, walking forward from {@link stateAtStartOfDayForgiving}
   * so gaps / past mistakes do not erase the schedule. Falls back to a full forgiving replay when
   * still in before-Phase-2.
   */
  function nextProjectedRestForwardCalendar(boundaryISO, mark, todayISO, st) {
    if (st.beforePhase2 || st.violation) return null;
    let targetL = st.targetL;
    let run = st.run;
    let needRest = st.needRest;
    let day = todayISO;
    for (let iter = 0; iter < 500; iter++) {
      if (needRest) {
        if (mark.has(day)) return null;
        return day;
      }
      let done;
      if (daysDiff(day, todayISO) >= 0) {
        done = mark.has(day);
      } else {
        done = true;
      }
      if (done) {
        run++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) return null;
          needRest = true;
        }
      } else {
        run = 0;
      }
      day = addDaysISO(day, 1);
    }
    return null;
  }

  /**
   * Next mandatory rest **assuming you stay on schedule**: past/today use actual marks; future streak
   * days are treated as logged so we can show a forward rest date before those days exist.
   * Past logs on historical mandatory rest slots are ignored for this projection only (see forgiving state).
   */
  function nextProjectedRestISO(boundaryISO, marksSet, todayISO, habitStartISO) {
    const mark = new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    if (daysDiff(firstP2, todayISO) >= 0) {
      const st = stateAtStartOfDayForgiving(boundaryISO, mark, todayISO, habitStartISO);
      const forward = nextProjectedRestForwardCalendar(boundaryISO, mark, todayISO, st);
      if (forward) return forward;
    }

    let day = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let complete = false;
    const maxScan = addDaysISO(todayISO, 500);
    let guard = 0;

    while (daysDiff(day, maxScan) >= 0 && !complete && guard < 12000) {
      guard++;
      let done;
      if (needRest) {
        done = mark.has(day);
        if (done && daysDiff(day, todayISO) > 0) {
          done = false;
        }
      } else if (daysDiff(day, todayISO) >= 0) {
        done = mark.has(day);
      } else {
        done = true;
      }

      if (needRest) {
        if (done) {
          return null;
        }
        if (daysDiff(day, todayISO) >= 0) {
          return day;
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else if (done) {
        run++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) {
            complete = true;
          } else {
            needRest = true;
          }
        }
      } else {
        run = 0;
      }
      day = addDaysISO(day, 1);
    }
    return null;
  }

  function nextProjectedRestForwardWorknight(boundaryISO, mark, todayISO, cheatFrozenSuns, st) {
    const cheatFrozen =
      cheatFrozenSuns instanceof Set ? cheatFrozenSuns : new Set(cheatFrozenSuns || []);
    if (st.beforePhase2 || st.violation) return null;
    let targetL = st.targetL;
    let run = st.run;
    let needRest = st.needRest;
    let night = firstSunThruThuOnOrAfter(todayISO);
    for (let iter = 0; iter < 500; iter++) {
      const sun = sundayOfWeekContaining(night);
      const frozen = cheatFrozen.has(sun);
      if (needRest) {
        if (mark.has(night)) return null;
        return night;
      }
      let done;
      if (daysDiff(night, todayISO) >= 0) {
        done = mark.has(night);
      } else {
        done = frozen ? mark.has(night) : true;
      }
      if (done) {
        run++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) return null;
          needRest = true;
        }
      } else if (!frozen) {
        run = 0;
      }
      night = nextSunThruThuAfter(night);
    }
    return null;
  }

  /** Worknight: same on-schedule projection (Sun–Thu chain; Fri/Sat skipped by night iterator). */
  function nextProjectedRestISOWorknight(boundaryISO, marksSet, todayISO, cheatFrozenSuns, habitStartISO) {
    const mark = new Set(marksSet);
    const cheatFrozen =
      cheatFrozenSuns instanceof Set ? cheatFrozenSuns : new Set(cheatFrozenSuns || []);
    const firstP2 = firstSunThruThuOnOrAfter(addDaysISO(boundaryISO, 1));
    if (daysDiff(firstP2, todayISO) >= 0) {
      const st = stateAtStartOfDayWorknightForgiving(boundaryISO, mark, todayISO, cheatFrozen, habitStartISO);
      const forward = nextProjectedRestForwardWorknight(boundaryISO, mark, todayISO, cheatFrozen, st);
      if (forward) return forward;
    }

    let night = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let complete = false;
    const maxScan = addDaysISO(todayISO, 500);
    let guard = 0;

    while (daysDiff(night, maxScan) >= 0 && !complete && guard < 12000) {
      guard++;
      const sun = sundayOfWeekContaining(night);
      const frozen = cheatFrozen.has(sun);
      let done;
      if (needRest) {
        done = mark.has(night);
        if (done && daysDiff(night, todayISO) > 0) {
          done = false;
        }
      } else if (daysDiff(night, todayISO) >= 0) {
        done = mark.has(night);
      } else {
        done = frozen ? mark.has(night) : true;
      }

      if (needRest) {
        if (done) {
          return null;
        }
        if (daysDiff(night, todayISO) >= 0) {
          return night;
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else if (done) {
        run++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) {
            complete = true;
          } else {
            needRest = true;
          }
        }
      } else if (!frozen) {
        run = 0;
      }
      night = nextSunThruThuAfter(night);
    }
    return null;
  }

  function simulatePhase2Worknight(boundaryISO, marksSet, maxScanISO, cheatFrozenSuns) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    let night = firstSunThruThuOnOrAfter(addDaysISO(boundaryISO, 1));
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let phase2Earned = 0;
    let complete = false;
    let violation = false;

    let guard = 0;
    while (daysDiff(night, maxScanISO) >= 0 && !complete && guard < 12000) {
      guard++;
      const sun = sundayOfWeekContaining(night);
      const frozen = cheatFrozenSuns.has(sun);
      const done = mark.has(night);
      if (needRest) {
        if (done) {
          violation = true;
          break;
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else if (done) {
        run++;
        phase2Earned++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) {
            complete = true;
          } else {
            needRest = true;
          }
        }
      } else if (!frozen) {
        run = 0;
      }
      night = nextSunThruThuAfter(night);
    }

    return { phase2Earned, complete, targetL, run, needRest, violation };
  }

  function phase2MarksSunThruThuAsc(boundaryISO, marksSet) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    const out = [];
    for (const iso of mark) {
      if (daysDiff(firstP2, iso) >= 0 && isSunThruThu(iso)) out.push(iso);
    }
    out.sort();
    return out;
  }

  function contiguousRunsWorknight(sortedAsc) {
    if (!sortedAsc.length) return [];
    const runs = [];
    let runStart = sortedAsc[0];
    let prev = sortedAsc[0];
    let len = 1;
    for (let i = 1; i < sortedAsc.length; i++) {
      const iso = sortedAsc[i];
      if (nextSunThruThuAfter(prev) === iso) {
        len += 1;
      } else {
        runs.push({ start: runStart, end: prev, length: len });
        runStart = iso;
        len = 1;
      }
      prev = iso;
    }
    runs.push({ start: runStart, end: prev, length: len });
    return runs;
  }

  function maxPhase2RunLengthWorknight(boundaryISO, marksSet) {
    const asc = phase2MarksSunThruThuAsc(boundaryISO, marksSet);
    const runs = contiguousRunsWorknight(asc);
    let m = 0;
    for (const r of runs) if (r.length > m) m = r.length;
    return m;
  }

  function terminalPhase2RunLengthWorknight(boundaryISO, marksSet) {
    const asc = phase2MarksSunThruThuAsc(boundaryISO, marksSet);
    const runs = contiguousRunsWorknight(asc);
    return runs.length ? runs[runs.length - 1].length : 0;
  }

  function simulatePhase2ForgivingWorknight(boundaryISO, marksSet) {
    const maxLen = maxPhase2RunLengthWorknight(boundaryISO, marksSet);
    if (maxLen >= 90) {
      return { forgivingPhase2Pts: PHASE2_MAX_POINTS, forgivingComplete: true };
    }
    const R = terminalPhase2RunLengthWorknight(boundaryISO, marksSet);
    if (R <= 0) {
      return { forgivingPhase2Pts: 0, forgivingComplete: false };
    }
    let pos = 0;
    let m = 8;
    let earned = 0;
    while (m <= 90) {
      const rem = R - pos;
      if (rem >= m) {
        pos += m;
        earned += m;
        m += 1;
      } else {
        earned += rem;
        break;
      }
    }
    const forgivingComplete = earned >= PHASE2_MAX_POINTS;
    const capped = Math.min(PHASE2_MAX_POINTS, earned);
    return { forgivingPhase2Pts: capped, forgivingComplete };
  }

  function deriveRestDaySetWorknight(boundaryISO, marksSet, cheatFrozenSuns, habitStartISO) {
    const set = new Set();
    const mark = new Set(marksSet);
    const cheatFrozen =
      cheatFrozenSuns instanceof Set ? cheatFrozenSuns : new Set(cheatFrozenSuns || []);
    const firstP2 = firstSunThruThuOnOrAfter(addDaysISO(boundaryISO, 1));
    let night = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let complete = false;
    const maxScanISO = addDaysISO(todayISO(), 365 * 5);
    let guard = 0;
    while (daysDiff(night, maxScanISO) >= 0 && !complete && guard < 12000) {
      guard++;
      const sun = sundayOfWeekContaining(night);
      const frozen = cheatFrozen.has(sun);
      const done = mark.has(night);
      if (needRest) {
        set.add(night);
        if (done) break;
        needRest = false;
        if (targetL < 90) targetL++;
      } else if (done) {
        run++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) complete = true;
          else needRest = true;
        }
      } else if (!frozen) {
        run = 0;
      }
      night = nextSunThruThuAfter(night);
    }
    return set;
  }

  function stateAtStartOfDayWorknight(boundaryISO, marksSet, dayISO, cheatFrozenSuns) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const firstP2 = firstSunThruThuOnOrAfter(addDaysISO(boundaryISO, 1));
    if (daysDiff(dayISO, firstP2) < 0) {
      return {
        targetL: 8,
        run: 0,
        needRest: false,
        violation: false,
        beforePhase2: true,
      };
    }
    let night = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let violation = false;
    let guard = 0;
    while (daysDiff(night, dayISO) < 0 && !violation && guard < 12000) {
      guard++;
      const sun = sundayOfWeekContaining(night);
      const frozen = cheatFrozenSuns.has(sun);
      const done = mark.has(night);
      if (needRest) {
        if (done) {
          violation = true;
          break;
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else if (done) {
        run++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) {
            /* complete */
          } else {
            needRest = true;
          }
        }
      } else if (!frozen) {
        run = 0;
      }
      night = nextSunThruThuAfter(night);
    }
    return { targetL, run, needRest, violation, beforePhase2: false };
  }

  /** Forgiving variant of {@link stateAtStartOfDayWorknight} for UI (see {@link stateAtStartOfDayForgiving}). */
  function stateAtStartOfDayWorknightForgiving(boundaryISO, marksSet, dayISO, cheatFrozenSuns, habitStartISO) {
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const cheatFrozen =
      cheatFrozenSuns instanceof Set ? cheatFrozenSuns : new Set(cheatFrozenSuns || []);
    const firstP2 = firstSunThruThuOnOrAfter(addDaysISO(boundaryISO, 1));
    if (daysDiff(dayISO, firstP2) < 0) {
      return {
        targetL: 8,
        run: 0,
        needRest: false,
        violation: false,
        beforePhase2: true,
      };
    }
    let night = firstP2;
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let violation = false;
    let guard = 0;
    while (daysDiff(night, dayISO) < 0 && !violation && guard < 12000) {
      guard++;
      const sun = sundayOfWeekContaining(night);
      const frozen = cheatFrozen.has(sun);
      let done = mark.has(night);
      if (needRest) {
        if (done) {
          if (daysDiff(night, dayISO) < 0) {
            done = false;
          } else {
            violation = true;
            break;
          }
        }
        needRest = false;
        if (targetL < 90) targetL++;
      } else if (done) {
        run++;
        if (run === targetL) {
          run = 0;
          if (targetL === 90) {
            /* complete */
          } else {
            needRest = true;
          }
        }
      } else if (!frozen) {
        run = 0;
      }
      night = nextSunThruThuAfter(night);
    }
    return { targetL, run, needRest, violation, beforePhase2: false };
  }

  function formatScheduleDay(iso) {
    return parseISODate(iso).toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  }

  /** Phase-2 leg badge: L8–L90 (matches ladder target length). */
  function formatLegLabel(targetLen) {
    if (typeof targetLen !== "number" || targetLen < 8 || targetLen > 90) return null;
    return `L${targetLen}`;
  }

  /**
   * @param {{ targetL: number, run: number, needRest: boolean, violation: boolean, beforePhase2: boolean }} st
   * @returns {{ label: string | null, detail: string | null }}
   */
  function longRunLegFromState(st) {
    if (!st || st.violation) {
      return { label: null, detail: null };
    }
    if (st.beforePhase2) {
      return { label: "L8", detail: "First long-run leg (L8) begins on your first Phase 2 day." };
    }
    if (st.needRest) {
      if (st.targetL >= 90) {
        return { label: "L90", detail: "Final leg finished." };
      }
      const cur = formatLegLabel(st.targetL);
      const nxt = formatLegLabel(st.targetL + 1);
      return {
        label: cur,
        detail: `Mandatory rest after ${cur}.${nxt ? ` Next leg: ${nxt}.` : ""}`,
      };
    }
    return { label: formatLegLabel(st.targetL), detail: null };
  }

  /**
   * Progress slice for the points row, e.g. "3/8" (streak run / leg target length). Uses forgiving state at
   * the UI leg anchor day: an open calendar today does not reset the streak; once today is marked, the streak
   * includes it. At mandatory rest after completing a leg, "n/n"; omitted before calendar Phase 2 or on violation.
   */
  function longRunLegCounterFromState(st) {
    if (!st || st.violation || st.beforePhase2) return null;
    if (st.needRest) return `${st.targetL}/${st.targetL}`;
    return `${st.run}/${st.targetL}`;
  }

  /**
   * Latest day this habit must be logged to keep the phase-1 weekly quota.
   *
   * Walks the earliest unsatisfied week (skipping weeks whose calendar window
   * has already passed) and returns the latest eligible un-logged day where
   * starting still leaves exactly enough days to meet that week's quota. If
   * not enough eligible days remain in the current/upcoming week, returns
   * todayISO with backlog=true (must log immediately to even attempt the
   * remaining weeks).
   *
   * @returns {{ iso: string, backlog: boolean } | null}
   */
  function nextHabitDayPhase1(h, actual, startISO, todayISO) {
    const cursor = todayISO > startISO ? todayISO : startISO;
    for (let w = 0; w < 7; w++) {
      const need = w + 1;
      const done = actual[w];
      if (done >= need) continue;
      const { weekStart, weekEnd } = phase1WeekRange(startISO, w);
      if (cursor > weekEnd) continue;
      let eligibleStart = weekStart;
      if (cursor > eligibleStart) eligibleStart = cursor;
      if (startISO > eligibleStart) eligibleStart = startISO;
      if (eligibleStart > weekEnd) continue;
      const avail = [];
      let d = eligibleStart;
      while (d <= weekEnd) {
        if (!h.days[d]) avail.push(d);
        d = addDaysISO(d, 1);
      }
      const remaining = need - done;
      if (avail.length < remaining) {
        return { iso: cursor, backlog: true };
      }
      return { iso: avail[avail.length - remaining], backlog: false };
    }
    return null;
  }

  /** Worknight Phase 1: planner-style pressure uses Sun–Thu only; skips cheat-frozen weeks. */
  function nextHabitDayPhase1Worknight(h, actual, startISO, todayStr, cheatSet) {
    const cursor = todayStr > startISO ? todayStr : startISO;
    for (let w = 0; w < PHASE1_WEEKS_WORKNIGHT; w++) {
      if (weekHasCheat(startISO, w, cheatSet)) continue;
      const need = w + 1;
      const done = actual[w];
      if (done >= need) continue;
      const { weekStart, weekEnd } = phase1WeekRange(startISO, w);
      if (cursor > weekEnd) continue;
      let eligibleStart = weekStart;
      if (cursor > eligibleStart) eligibleStart = cursor;
      if (startISO > eligibleStart) eligibleStart = startISO;
      if (eligibleStart > weekEnd) continue;
      const avail = [];
      let d = eligibleStart;
      while (d <= weekEnd) {
        if (isSunThruThu(d) && !h.days[d]) avail.push(d);
        d = addDaysISO(d, 1);
      }
      const remaining = need - done;
      if (avail.length < remaining) {
        return { iso: cursor, backlog: true };
      }
      return { iso: avail[avail.length - remaining], backlog: false };
    }
    return null;
  }

  function nextRestFromSet(restDays, todayISO) {
    let best = null;
    for (const r of restDays) {
      if (daysDiff(todayISO, r) < 0) continue;
      if (best === null || r < best) best = r;
    }
    return best;
  }

  function nextHabitDayPhase2(boundaryISO, marksSet, todayISO, programComplete, habitStartISO) {
    if (programComplete) return null;
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    let d = todayISO;
    if (daysDiff(d, firstP2) < 0) {
      d = firstP2;
    }
    for (let i = 0; i < 800; i++) {
      const st = stateAtStartOfDayForgiving(boundaryISO, mark, d, habitStartISO);
      if (st.violation) return null;
      if (st.needRest) {
        if (mark.has(d)) return null;
        d = addDaysISO(d, 1);
        continue;
      }
      if (!mark.has(d)) return d;
      d = addDaysISO(d, 1);
    }
    return null;
  }

  function nextHabitDayPhase2Worknight(
    boundaryISO,
    marksSet,
    todayStr,
    programComplete,
    cheatFrozenSuns,
    habitStartISO,
  ) {
    if (programComplete) return null;
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const cheatFrozen =
      cheatFrozenSuns instanceof Set ? cheatFrozenSuns : new Set(cheatFrozenSuns || []);
    const firstP2 = firstSunThruThuOnOrAfter(addDaysISO(boundaryISO, 1));
    let d = todayStr;
    if (daysDiff(d, firstP2) < 0) {
      d = firstP2;
    }
    for (let i = 0; i < 800; i++) {
      if (!isSunThruThu(d)) {
        d = addDaysISO(d, 1);
        continue;
      }
      const st = stateAtStartOfDayWorknightForgiving(boundaryISO, mark, d, cheatFrozen, habitStartISO);
      if (st.violation) return null;
      if (st.needRest) {
        if (mark.has(d)) return null;
        d = addDaysISO(d, 1);
        continue;
      }
      if (!mark.has(d)) return d;
      d = addDaysISO(d, 1);
    }
    return null;
  }

  function deriveHabit(h) {
    if (isWorknight(h)) return deriveHabitWorknight(h);
    const marks = markedDates(h);
    const marksSet = new Set(marks);
    const { actual, phase1Earned, phase1Satisfied } = phase1Stats(h.start, marks);

    const boundary = phase1Satisfied ? phase2BoundaryDate(h.start, marks) : null;
    const maxISO = maxDerivedScanISO(h, boundary);
    let phase2Earned = 0;
    let strictPct = (100 * phase1Earned) / TOTAL_POINTS;
    let forgivingPct = (100 * phase1Earned) / TOTAL_POINTS;
    let strictComplete = false;
    let forgivingPhase2Pts = 0;
    let forgivingComplete = false;
    let sim = null;
    let restDays = new Set();

    if (boundary !== null) {
      sim = simulatePhase2(boundary, marksSet, maxISO);
      phase2Earned = sim.phase2Earned;
      strictComplete = sim.complete;
      if (!sim.violation) {
        strictPct = (100 * (phase1Earned + phase2Earned)) / TOTAL_POINTS;
        if (strictComplete) strictPct = 100;
      } else {
        strictPct = (100 * (phase1Earned + phase2Earned)) / TOTAL_POINTS;
      }

      const f = simulatePhase2Forgiving(boundary, marksSet);
      forgivingPhase2Pts = f.forgivingPhase2Pts;
      forgivingComplete = f.forgivingComplete;
      forgivingPhase2Pts = Math.max(forgivingPhase2Pts, phase2Earned);
      forgivingPct = (100 * (phase1Earned + forgivingPhase2Pts)) / TOTAL_POINTS;
      if (forgivingComplete) forgivingPct = 100;

      restDays = deriveRestDaySet(boundary, marksSet, h.start);
    }

    const phase2StartISO = boundary !== null ? addDaysISO(boundary, 1) : null;
    const nominalRestISOList = phase2StartISO ? nominalPhase2RestDates(phase2StartISO) : [];
    const nominalRestDays = new Set(nominalRestISOList);

    const programDone = Boolean(strictComplete || forgivingComplete);

    const today = todayISO();
    const tomorrow = addDaysISO(today, 1);
    /** Leg/points row: open local "today" must not reset streak; after today is logged, include it. */
    const phase2LegUiDayISO = marksSet.has(today) ? tomorrow : today;
    let journeyPhase = "phase1";
    if (programDone) journeyPhase = "done";
    else if (phase1Satisfied && boundary !== null) journeyPhase = "phase2";

    /** Nominal-rest-window leg counter (calendar Phase 2); null on strict violation. */
    let nominalLegProgress = null;
    if (
      journeyPhase === "phase2" &&
      boundary !== null &&
      phase2StartISO &&
      !(sim && sim.violation)
    ) {
      nominalLegProgress = phase2LegProgressFromNominalRests(phase2StartISO, marksSet, today);
    }

    let longRunLegLabel = null;
    let longRunLegDetail = null;
    let longRunLegCounter = null;
    if (nominalLegProgress) {
      longRunLegLabel = nominalLegProgress.label;
      longRunLegCounter = nominalLegProgress.counter;
      longRunLegDetail = null;
    }

    let nextRestLabel = "—";
    let nextHabitLabel = "—";
    let nextHabitISO = null;
    let nextRestScheduledISO = null;
    let status = "";
    const p2StatusPrefix =
      journeyPhase === "phase2" && longRunLegLabel ? `Phase 2 · ${longRunLegLabel} · ` : "";

    if (!phase1Satisfied) {
      const w = phase1CurrentWeekUiIndex(h.start, today, actual);
      const need = w + 1;
      const incompleteEarlier = [];
      for (let i = 0; i < w; i++) {
        const n = i + 1;
        if (actual[i] < n) incompleteEarlier.push(`${i + 1} (${actual[i]}/${n})`);
      }
      const backlog =
        incompleteEarlier.length > 0 ? ` · catch up earlier: ${incompleteEarlier.join("; ")}` : "";
      status = `Week ${w + 1} of 7 · ${actual[w]}/${need} days logged this calendar week${backlog}.`;
    } else if (boundary === null) {
      status = "Phase 1 complete (boundary error)";
    } else if (programDone) {
      if (forgivingComplete && !strictComplete) {
        status =
          "Complete — Progress track (e.g. 90-day streak or milestones). Long-run ladder may stay below 100%.";
      } else if (strictComplete) {
        status = "Complete — Long-run Phase 2 ladder finished.";
      } else {
        status = "Complete.";
      }
    } else if (sim && sim.violation) {
      status =
        "Phase 2 (Long run): you logged on a required rest day. Fix rests to advance the Long-run bar; Progress bar still reflects your latest streak.";
    } else if (sim) {
      const projectedRestIso = nextProjectedRestISO(boundary, marksSet, today, h.start);
      const stEod = stateAtStartOfDayForgiving(boundary, marksSet, phase2LegUiDayISO, h.start);
      if (stEod.violation) {
        status =
          "Phase 2 (Long run): you logged on a required rest day. Fix rests to advance the Long-run bar; Progress bar still reflects your latest streak.";
      } else if (stEod.beforePhase2) {
        const firstP2 = addDaysISO(boundary, 1);
        const stP2 = stateAtStartOfDayForgiving(boundary, marksSet, firstP2, h.start);
        const t = formatLegLabel(stP2.targetL) || "L8";
        const mid = stP2.needRest
          ? `Next Phase 2 calendar day (${formatScheduleDay(firstP2)}) is a mandatory rest.`
          : `First Phase 2 day ${formatScheduleDay(firstP2)}: long-run streak ${stP2.run}/${stP2.targetL} toward ${t}.`;
        status = `${p2StatusPrefix}${mid} Progress bar uses your latest contiguous streak.`;
      } else if (stEod.needRest) {
        status = `${p2StatusPrefix}${
          longRunLegDetail ? `${longRunLegDetail}. ` : ""
        }Leave the next calendar day unchecked (rest). After that rest, the next leg targets ${
          stEod.targetL + 1
        } days. Strict Long-run points already banked are kept.`;
      } else {
        status = `${p2StatusPrefix}Long run: streak target ${stEod.targetL}; streak ${stEod.run}/${
          stEod.targetL
        } (through end of today). Next mandatory rest if you stay on schedule: ${
          projectedRestIso ? formatScheduleDay(projectedRestIso) : "—"
        }. Progress bar uses your latest contiguous streak.`;
      }
    }

    if (daysDiff(today, h.start) > 0) {
      nextHabitISO = h.start;
      nextHabitLabel = `${formatScheduleDay(h.start)} (habit hasn’t started)`;
      nextRestLabel = "— (streak rests start in phase 2)";
    } else if (!phase1Satisfied) {
      const nh = nextHabitDayPhase1(h, actual, h.start, today);
      nextRestLabel = "— (phase 1 has no rest days)";
      if (nh) {
        nextHabitISO = nh.iso;
        nextHabitLabel = nh.backlog
          ? `${formatScheduleDay(nh.iso)} — log today; remaining days are short of this week's quota`
          : formatScheduleDay(nh.iso);
      }
    } else if (boundary === null) {
      nextRestLabel = "—";
      nextHabitLabel = "—";
    } else if (programDone) {
      nextRestLabel = "None (finished)";
      nextHabitLabel = "None (finished)";
    } else if (sim && sim.violation) {
      nextRestLabel = "—";
      nextHabitLabel = "— (fix strict rest violation to use strict next-day hints)";
    } else {
      let nr = nextRestFromSet(restDays, today);
      if (!nr) nr = nextNeedRestStartISO(boundary, marksSet, today, h.start);
      if (!nr) nr = nextProjectedRestISO(boundary, marksSet, today, h.start);
      nextRestScheduledISO = nr;
      nextRestLabel = nr ? `${formatScheduleDay(nr)} (leave unchecked)` : "—";
      const nh = nextHabitDayPhase2(boundary, marksSet, today, programDone, h.start);
      if (nh) {
        nextHabitISO = nh;
        nextHabitLabel =
          daysDiff(today, nh) === 0 ? `${formatScheduleDay(nh)} (today)` : formatScheduleDay(nh);
      } else {
        nextHabitLabel = "—";
      }
    }

    const pct = Math.min(100, Math.max(0, forgivingPct));

    const phaseSlug =
      journeyPhase === "done" ? "done" : journeyPhase === "phase2" ? "phase2" : "phase1";
    let curTarget = null;
    let curRun = null;
    if (nominalLegProgress) {
      curTarget = nominalLegProgress.targetLen;
      curRun = nominalLegProgress.run;
    }

    const programState = {
      phase: phaseSlug,
      anchor_iso: today,
      phase1: {
        actual_per_week: actual.slice(),
        satisfied: phase1Satisfied,
        current_week_ui_index: phase1CurrentWeekUiIndex(h.start, today, actual),
        points_earned: phase1Earned,
        points_cap: 28,
      },
      phase2:
        boundary !== null && phase2StartISO
          ? {
              boundary_iso: boundary,
              phase2_start_iso: phase2StartISO,
              nominal_rest_dates: nominalRestISOList.slice(),
              strict: {
                violation: Boolean(sim && sim.violation),
                complete: strictComplete,
                earned_points: phase2Earned,
              },
              forgiving: {
                points: forgivingPhase2Pts,
                complete: forgivingComplete,
              },
              effective_next_rest_iso: nextRestScheduledISO,
              current_leg_target_len: curTarget,
              current_leg_run_start_of_tomorrow: curRun,
            }
          : null,
    };

    return {
      phase1Earned,
      phase2Earned,
      forgivingPhase2Pts,
      pct,
      strictPct: Math.min(100, Math.max(0, strictPct)),
      forgivingPct: Math.min(100, Math.max(0, forgivingPct)),
      strictComplete,
      forgivingComplete,
      programDone,
      status,
      complete: programDone,
      boundary,
      phase1Satisfied,
      restDays,
      sim,
      nextRestLabel,
      nextHabitLabel,
      nextHabitISO,
      nextRestScheduledISO,
      journeyPhase,
      longRunLegLabel,
      longRunLegDetail,
      longRunLegCounter,
      phase1PointsCap: 28,
      nominalRestDays,
      nominalRestISOList,
      programState,
    };
  }

  function deriveHabitWorknight(h) {
    const marks = markedDates(h);
    const marksSet = new Set(marks);
    const cheatSet = cheatDatesAsSet(h);
    const cheatFrozenSuns = cheatFrozenSundaySet(h);
    const { actual, phase1Earned, phase1Satisfied } = phase1StatsWorknight(h.start, marks, cheatSet);

    const boundary = phase1Satisfied ? phase2BoundaryDateWorknight(h.start, marks) : null;
    const maxISO = maxDerivedScanISO(h, boundary);
    let phase2Earned = 0;
    let strictPct = (100 * phase1Earned) / TOTAL_POINTS_WORKNIGHT;
    let forgivingPct = (100 * phase1Earned) / TOTAL_POINTS_WORKNIGHT;
    let strictComplete = false;
    let forgivingPhase2Pts = 0;
    let forgivingComplete = false;
    let sim = null;
    let restDays = new Set();

    if (boundary !== null) {
      sim = simulatePhase2Worknight(boundary, marksSet, maxISO, cheatFrozenSuns);
      phase2Earned = sim.phase2Earned;
      strictComplete = sim.complete;
      if (!sim.violation) {
        strictPct = (100 * (phase1Earned + phase2Earned)) / TOTAL_POINTS_WORKNIGHT;
        if (strictComplete) strictPct = 100;
      } else {
        strictPct = (100 * (phase1Earned + phase2Earned)) / TOTAL_POINTS_WORKNIGHT;
      }

      const f = simulatePhase2ForgivingWorknight(boundary, marksSet);
      forgivingPhase2Pts = f.forgivingPhase2Pts;
      forgivingComplete = f.forgivingComplete;
      forgivingPhase2Pts = Math.max(forgivingPhase2Pts, phase2Earned);
      forgivingPct = (100 * (phase1Earned + forgivingPhase2Pts)) / TOTAL_POINTS_WORKNIGHT;
      if (forgivingComplete) forgivingPct = 100;

      restDays = deriveRestDaySetWorknight(boundary, marksSet, cheatFrozenSuns, h.start);
    }

    const phase2StartISO =
      boundary !== null ? firstSunThruThuOnOrAfter(addDaysISO(boundary, 1)) : null;
    const nominalRestISOList = [];
    const nominalRestDays = new Set();

    const programDone = Boolean(strictComplete || forgivingComplete);

    const today = todayISO();
    const tomorrow = addDaysISO(today, 1);
    /** Leg/points row: open local "today" must not reset streak; after today is logged, include it. */
    const phase2LegUiDayISO = marksSet.has(today) ? tomorrow : today;
    let journeyPhase = "phase1";
    if (programDone) journeyPhase = "done";
    else if (phase1Satisfied && boundary !== null) journeyPhase = "phase2";

    let longRunLegLabel = null;
    let longRunLegDetail = null;
    let longRunLegCounter = null;
    if (journeyPhase === "phase2" && boundary !== null) {
      const stLeg = stateAtStartOfDayWorknightForgiving(
        boundary,
        marksSet,
        phase2LegUiDayISO,
        cheatFrozenSuns,
        h.start
      );
      longRunLegCounter = longRunLegCounterFromState(stLeg);
      if (longRunLegCounter == null && stLeg.beforePhase2) {
        const fp2 = firstSunThruThuOnOrAfter(addDaysISO(boundary, 1));
        longRunLegCounter = longRunLegCounterFromState(
          stateAtStartOfDayWorknightForgiving(boundary, marksSet, fp2, cheatFrozenSuns, h.start)
        );
      }
      const leg = longRunLegFromState(stLeg);
      longRunLegLabel = leg.label;
      longRunLegDetail = leg.detail;
      if (stLeg.violation) {
        longRunLegLabel = null;
        longRunLegDetail = null;
        longRunLegCounter = null;
      }
    }

    let status = "";
    const p2StatusPrefix =
      journeyPhase === "phase2" && longRunLegLabel ? `Phase 2 · ${longRunLegLabel} · ` : "";

    if (!phase1Satisfied) {
      const w = phase1CurrentWeekUiIndexWorknight(h.start, today, actual, cheatSet);
      const need = w + 1;
      const incompleteEarlier = [];
      for (let i = 0; i < w; i++) {
        const n = i + 1;
        const ok = actual[i] >= n || weekHasCheat(h.start, i, cheatSet);
        if (!ok) incompleteEarlier.push(`${i + 1} (${actual[i]}/${n})`);
      }
      const backlog =
        incompleteEarlier.length > 0 ? ` · catch up earlier: ${incompleteEarlier.join("; ")}` : "";
      const freeze = weekHasCheat(h.start, w, cheatSet) ? " · cheat freeze active this week" : "";
      status = `Week ${w + 1} of 5 · ${actual[w]}/${need} logs (Fri/Sat optional; Sun–Thu set planner pressure)${freeze}${backlog}.`;
    } else if (boundary === null) {
      status = "Phase 1 complete (boundary error)";
    } else if (programDone) {
      if (forgivingComplete && !strictComplete) {
        status =
          "Complete — Progress track (90 work nights or milestones). Long-run ladder may stay below 100%.";
      } else if (strictComplete) {
        status = "Complete — Long-run Phase 2 ladder finished (Sun–Thu nights; Fri/Sat skipped).";
      } else {
        status = "Complete.";
      }
    } else if (sim && sim.violation) {
      status =
        "Phase 2 (Long run): you logged on a required rest night. Fix rests to advance the Long-run bar; Progress bar still reflects your latest streak.";
    } else if (sim) {
      const projectedRestIso = nextProjectedRestISOWorknight(
        boundary,
        marksSet,
        today,
        cheatFrozenSuns,
        h.start
      );
      const stEod = stateAtStartOfDayWorknightForgiving(
        boundary,
        marksSet,
        phase2LegUiDayISO,
        cheatFrozenSuns,
        h.start
      );
      if (stEod.violation) {
        status =
          "Phase 2 (Long run): you logged on a required rest night. Fix rests to advance the Long-run bar; Progress bar still reflects your latest streak.";
      } else if (stEod.beforePhase2) {
        const firstP2 = firstSunThruThuOnOrAfter(addDaysISO(boundary, 1));
        const stP2 = stateAtStartOfDayWorknightForgiving(
          boundary,
          marksSet,
          firstP2,
          cheatFrozenSuns,
          h.start
        );
        const t = formatLegLabel(stP2.targetL) || "L8";
        const mid = stP2.needRest
          ? `Next Phase 2 Sun–Thu night (${formatScheduleDay(firstP2)}) is a mandatory rest.`
          : `First Phase 2 night ${formatScheduleDay(firstP2)}: long-run streak ${stP2.run}/${stP2.targetL} toward ${t}.`;
        status = `${p2StatusPrefix}${mid}`;
      } else if (stEod.needRest) {
        status = `${p2StatusPrefix}${
          longRunLegDetail ? `${longRunLegDetail}. ` : ""
        }Leave your next Sun–Thu night unchecked (rest). After that rest, the next leg targets ${
          stEod.targetL + 1
        } nights. Strict Long-run points already banked are kept.`;
      } else {
        status = `${p2StatusPrefix}Long run: Sun–Thu nights only; target ${stEod.targetL}; streak ${
          stEod.run
        }/${stEod.targetL} (through end of today). Next mandatory rest if you stay on schedule: ${
          projectedRestIso ? formatScheduleDay(projectedRestIso) : "—"
        }.`;
      }
    }

    let nextRestLabel = "—";
    let nextHabitLabel = "—";
    let nextHabitISO = null;
    let nextRestScheduledISO = null;

    if (daysDiff(today, h.start) > 0) {
      nextHabitISO = h.start;
      nextHabitLabel = `${formatScheduleDay(h.start)} (habit hasn’t started)`;
      nextRestLabel = "— (streak rests start in phase 2)";
    } else if (!phase1Satisfied) {
      const nh = nextHabitDayPhase1Worknight(h, actual, h.start, today, cheatSet);
      nextRestLabel = "— (phase 1 has no rest days)";
      if (nh) {
        nextHabitISO = nh.iso;
        nextHabitLabel = nh.backlog
          ? `${formatScheduleDay(nh.iso)} — log today; Sun–Thu may be short for this week's quota`
          : formatScheduleDay(nh.iso);
      }
    } else if (boundary === null) {
      nextRestLabel = "—";
      nextHabitLabel = "—";
    } else if (programDone) {
      nextRestLabel = "None (finished)";
      nextHabitLabel = "None (finished)";
    } else if (sim && sim.violation) {
      nextRestLabel = "—";
      nextHabitLabel = "— (fix strict rest violation to use strict next-day hints)";
    } else {
      let nr = nextRestFromSet(restDays, today);
      if (!nr) nr = nextNeedRestStartISOWorknight(boundary, marksSet, today, cheatFrozenSuns, h.start);
      if (!nr) nr = nextProjectedRestISOWorknight(boundary, marksSet, today, cheatFrozenSuns, h.start);
      nextRestScheduledISO = nr;
      nextRestLabel = nr ? `${formatScheduleDay(nr)} (leave unchecked)` : "—";
      const nh = nextHabitDayPhase2Worknight(
        boundary,
        marksSet,
        today,
        programDone,
        cheatFrozenSuns,
        h.start
      );
      if (nh) {
        nextHabitISO = nh;
        nextHabitLabel =
          daysDiff(today, nh) === 0 ? `${formatScheduleDay(nh)} (today)` : formatScheduleDay(nh);
      } else {
        nextHabitLabel = "—";
      }
    }

    const pct = Math.min(100, Math.max(0, forgivingPct));

    const phaseSlugWn =
      journeyPhase === "done" ? "done" : journeyPhase === "phase2" ? "phase2" : "phase1";
    const stTomorrowWn =
      boundary !== null
        ? stateAtStartOfDayWorknightForgiving(
            boundary,
            marksSet,
            phase2LegUiDayISO,
            cheatFrozenSuns,
            h.start
          )
        : null;
    let curTargetWn = null;
    let curRunWn = null;
    if (stTomorrowWn && journeyPhase === "phase2") {
      if (!stTomorrowWn.violation) {
        if (stTomorrowWn.beforePhase2) {
          curTargetWn = 8;
          curRunWn = 0;
        } else {
          curTargetWn = stTomorrowWn.targetL;
          curRunWn = stTomorrowWn.run;
        }
      }
    }

    const programState = {
      phase: phaseSlugWn,
      anchor_iso: today,
      phase1: {
        actual_per_week: actual.slice(),
        satisfied: phase1Satisfied,
        current_week_ui_index: phase1CurrentWeekUiIndexWorknight(h.start, today, actual, cheatSet),
        points_earned: phase1Earned,
        points_cap: PHASE1_MAX_POINTS_WORKNIGHT,
      },
      phase2:
        boundary !== null && phase2StartISO
          ? {
              boundary_iso: boundary,
              phase2_start_iso: phase2StartISO,
              nominal_rest_dates: [],
              strict: {
                violation: Boolean(sim && sim.violation),
                complete: strictComplete,
                earned_points: phase2Earned,
              },
              forgiving: {
                points: forgivingPhase2Pts,
                complete: forgivingComplete,
              },
              effective_next_rest_iso: nextRestScheduledISO,
              current_leg_target_len: curTargetWn,
              current_leg_run_start_of_tomorrow: curRunWn,
            }
          : null,
    };

    return {
      phase1Earned,
      phase2Earned,
      forgivingPhase2Pts,
      pct,
      strictPct: Math.min(100, Math.max(0, strictPct)),
      forgivingPct: Math.min(100, Math.max(0, forgivingPct)),
      strictComplete,
      forgivingComplete,
      programDone,
      status,
      complete: programDone,
      boundary,
      phase1Satisfied,
      restDays,
      sim,
      nextRestLabel,
      nextHabitLabel,
      nextHabitISO,
      nextRestScheduledISO,
      journeyPhase,
      longRunLegLabel,
      longRunLegDetail,
      longRunLegCounter,
      phase1PointsCap: PHASE1_MAX_POINTS_WORKNIGHT,
      nominalRestDays,
      nominalRestISOList,
      programState,
    };
  }

  function habitListNextLogClause(derived) {
    if (derived.nextHabitISO) return `Next log: ${derived.nextHabitISO}`;
    if (derived.programDone) return "Next log: complete";
    return "Next log: —";
  }

  /**
   * @param {boolean} turningOn – true if click would mark the day done
   */
  function canToggleDay(h, iso, derived, turningOn) {
    if (daysDiff(h.start, iso) < 0) return { ok: false, reason: "before-start" };
    if (derived.restDays.has(iso) && turningOn) return { ok: false, reason: "rest-required" };
    return { ok: true };
  }

  function renderList() {
    closeHabitTypePopover();
    const ul = document.getElementById("habit-list");
    const count = document.getElementById("habit-list-count");
    if (count) count.textContent = `${habits.length} habit${habits.length === 1 ? "" : "s"}`;
    ul.innerHTML = "";
    if (habits.length === 0) {
      const hintEmpty = document.getElementById("habit-list-hint");
      if (hintEmpty) hintEmpty.hidden = true;
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "No habits yet — add one above.";
      ul.appendChild(li);
      return;
    }
    const hint = document.getElementById("habit-list-hint");
    if (hint) {
      if (habits.length === 0) {
        hint.hidden = true;
      } else {
        hint.hidden = false;
        hint.textContent =
          habits.length > 1
            ? "Click a habit to open details · click again to collapse."
            : "Add more habits anytime; each is stored separately (IndexedDB + local backup).";
      }
    }
    for (const h of habits) {
      const li = document.createElement("li");
      li.dataset.id = h.id;
      li.setAttribute("role", "button");
      li.setAttribute("tabindex", "0");
      li.setAttribute("aria-pressed", h.id === selectedId ? "true" : "false");
      if (h.id === selectedId) li.classList.add("active");
      const left = document.createElement("div");
      left.className = "habit-li-main";
      const titleRow = document.createElement("div");
      titleRow.className = "habit-li-title-row";
      const titleEl = document.createElement("span");
      titleEl.className = "habit-title";
      titleEl.textContent = h.title || "(untitled)";
      const gearBtn = document.createElement("button");
      gearBtn.type = "button";
      gearBtn.className = "habit-gear-btn";
      gearBtn.setAttribute("aria-label", "Habit settings");
      gearBtn.setAttribute("aria-haspopup", "dialog");
      gearBtn.setAttribute("aria-expanded", "false");
      gearBtn.innerHTML =
        '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
      gearBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (habitTypePopoverHabitId === h.id) closeHabitTypePopover();
        else openHabitTypePopover(h.id, gearBtn);
      });
      titleRow.appendChild(titleEl);
      titleRow.appendChild(gearBtn);
      const d = deriveHabit(h);
      const meta = document.createElement("span");
      meta.className = "muted habit-li-meta";
      const n = markedDates(h).length;
      meta.textContent = `${habitListNextLogClause(d)} · ${n} day(s) logged${isWorknight(h) ? " · Worknight" : ""}`;
      left.appendChild(titleRow);
      left.appendChild(meta);
      li.appendChild(left);
      const progress = document.createElement("div");
      progress.className = "habit-progress";
      const stack = document.createElement("div");
      stack.className = "habit-progress-rows";
      function habitMiniRow(fillClassName, pctVal) {
        const wrap = document.createElement("div");
        wrap.className = "habit-progress-row-wrap";
        const row = document.createElement("div");
        row.className = "mini-bar " + fillClassName;
        const fill = document.createElement("span");
        fill.style.width = `${pctVal}%`;
        row.appendChild(fill);
        const sp = document.createElement("span");
        sp.className = "muted habit-progress-pct";
        sp.textContent = `${formatProgressPct(pctVal)}%`;
        wrap.appendChild(row);
        wrap.appendChild(sp);
        return wrap;
      }
      stack.appendChild(habitMiniRow("forgiving", d.forgivingPct));
      stack.appendChild(habitMiniRow("strict-mini", d.strictPct));
      progress.title = "Progress track (accent) vs Long-run track (green)";
      progress.appendChild(stack);
      li.appendChild(progress);
      const selectHabit = () => {
        if (selectedId === h.id) {
          selectedId = null;
        } else {
          selectedId = h.id;
          const t = new Date();
          viewYear = t.getFullYear();
          viewMonth = t.getMonth();
        }
        save();
        render();
      };
      li.addEventListener("click", selectHabit);
      li.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectHabit();
        }
      });
      ul.appendChild(li);
    }
  }

  function renderDetail() {
    const detail = document.getElementById("detail");
    const h = habits.find((x) => x.id === selectedId);
    if (!h) {
      detail.hidden = true;
      return;
    }
    detail.hidden = false;
    document.getElementById("detail-title").textContent = h.title;
    const detailStart = document.getElementById("detail-start-input");
    detailStart.value = h.start;
    detailStart.disabled = false;
    detailStart.title = isWorknight(h)
      ? "Worknight Phase 1: five calendar weeks (Sun–Thu drive deadlines; Fri/Sat optional). Week 1 contains program start."
      : "Phase 1 weeks are Sun–Sat; week 1 is the calendar week that contains this date. Your first log must be this date or later.";
    const derived = deriveHabit(h);
    const detailStatusEl = document.getElementById("detail-status");
    const suppressDetailStatusFlavor =
      derived.journeyPhase === "phase2" &&
      !derived.programDone &&
      !(derived.sim && derived.sim.violation);
    if (suppressDetailStatusFlavor) {
      detailStatusEl.textContent = "";
      detailStatusEl.hidden = true;
    } else {
      detailStatusEl.hidden = false;
      detailStatusEl.textContent = derived.status;
    }
    const wnHint = document.getElementById("detail-worknight-hint");
    if (wnHint) {
      if (isWorknight(h)) {
        wnHint.hidden = false;
        wnHint.textContent =
          "Worknight: click once to log a night; click twice quickly on the same date for a cheat freeze (blue). Max 2 cheats per rolling 30 days. Phase 2 counts Sun–Thu streaks only (Thu→Sun is consecutive).";
      } else {
        wnHint.hidden = true;
        wnHint.textContent = "";
      }
    }

    document.getElementById("progress-pct-forgiving").textContent =
      `${formatProgressPct(derived.forgivingPct)}%`;
    document.getElementById("progress-pct-strict").textContent =
      `${formatProgressPct(derived.strictPct)}%`;

    const pointsRoot = document.getElementById("detail-points");
    pointsRoot.replaceChildren();
    function addPtsRow(label, value) {
      const row = document.createElement("div");
      row.className = "points-summary-row";
      const l = document.createElement("span");
      l.className = "points-label";
      l.textContent = label;
      const v = document.createElement("span");
      v.className = "points-value";
      v.textContent = value;
      row.appendChild(l);
      row.appendChild(v);
      pointsRoot.appendChild(row);
    }
    addPtsRow("Phase 1", `${derived.phase1Earned} / ${derived.phase1PointsCap ?? 28} pts`);
    addPtsRow("Phase 2 · Progress track", `${derived.forgivingPhase2Pts} / ${PHASE2_MAX_POINTS} pts`);
    const longRunLabel =
      derived.journeyPhase === "phase2" && derived.longRunLegLabel
        ? derived.longRunLegCounter
          ? `Phase 2 · Long-run track · ${derived.longRunLegLabel} · ${derived.longRunLegCounter}`
          : `Phase 2 · Long-run track · ${derived.longRunLegLabel}`
        : "Phase 2 · Long-run track";
    addPtsRow(longRunLabel, `${derived.phase2Earned} / ${PHASE2_MAX_POINTS} pts`);

    const upcoming = document.getElementById("detail-upcoming");
    upcoming.hidden = false;
    upcoming.replaceChildren();
    const ps = derived.programState;
    const todayStr = todayISO();

    if (!isWorknight(h) && ps?.phase2?.phase2_start_iso && derived.journeyPhase !== "done") {
      const rowP2 = document.createElement("div");
      const sp = document.createElement("strong");
      sp.textContent = "Phase 2 start";
      rowP2.appendChild(sp);
      rowP2.appendChild(
        document.createTextNode(` · ${formatScheduleDay(ps.phase2.phase2_start_iso)} (Leg 8 day 1)`)
      );
      upcoming.appendChild(rowP2);
    } else if (isWorknight(h) && ps?.phase2?.phase2_start_iso && derived.journeyPhase !== "done") {
      const rowP2 = document.createElement("div");
      const sp = document.createElement("strong");
      sp.textContent = "Phase 2 start";
      rowP2.appendChild(sp);
      rowP2.appendChild(
        document.createTextNode(
          ` · ${formatScheduleDay(ps.phase2.phase2_start_iso)} (first Sun–Thu night)`
        )
      );
      upcoming.appendChild(rowP2);
    }

    if (!isWorknight(h) && ps?.phase2?.nominal_rest_dates?.length) {
      const nom = ps.phase2.nominal_rest_dates;
      const upcomingNom = nom.filter((iso) => daysDiff(todayStr, iso) >= 0).slice(0, 1);
      if (upcomingNom.length) {
        const rowNom = document.createElement("div");
        const hn = document.createElement("strong");
        hn.textContent = "Next Rest";
        rowNom.appendChild(hn);
        rowNom.appendChild(
          document.createTextNode(` · ${formatScheduleDay(upcomingNom[0])}`)
        );
        upcoming.appendChild(rowNom);
      }
    }

    const rowHabit = document.createElement("div");
    const sh = document.createElement("strong");
    sh.textContent = "Next Day";
    rowHabit.appendChild(sh);
    rowHabit.appendChild(document.createTextNode(` · ${derived.nextHabitLabel}`));
    upcoming.appendChild(rowHabit);

    const fillS = document.getElementById("progress-fill-strict");
    const fillF = document.getElementById("progress-fill-forgiving");
    fillS.style.width = `${derived.strictPct}%`;
    fillF.style.width = `${derived.forgivingPct}%`;

    const first = new Date(viewYear, viewMonth, 1);
    const label = document.getElementById("cal-label");
    label.textContent = first.toLocaleString(undefined, { month: "long", year: "numeric" });

    const legDef = document.getElementById("cal-legend-default");
    const legWn = document.getElementById("cal-legend-worknight");
    if (legDef && legWn) {
      legDef.hidden = isWorknight(h);
      legWn.hidden = !isWorknight(h);
    }

    const root = document.getElementById("cal-root");
    root.innerHTML = "";
    const dows = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    for (const x of dows) {
      const e = document.createElement("div");
      e.className = "dow";
      e.textContent = x;
      root.appendChild(e);
    }

    const startDow = first.getDay();
    const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
    const prevAnchor = new Date(viewYear, viewMonth, 0);
    const prevYear = prevAnchor.getFullYear();
    const prevMonth1 = prevAnchor.getMonth() + 1;
    const prevMonthDays = prevAnchor.getDate();

    function appendDayCell(iso, displayNum, adjacentMonth) {
      const cell = document.createElement("div");
      cell.className = adjacentMonth ? "cell other" : "cell";
      cell.textContent = String(displayNum);
      const done = !!h.days[iso];
      if (done) cell.classList.add("done");
      const beforeStart = daysDiff(h.start, iso) < 0;
      if (beforeStart) cell.classList.add("before-start");
      if (h.cheat_days && h.cheat_days[iso]) cell.classList.add("cheat-freeze");
      const effectiveRest =
        !done && (derived.restDays.has(iso) || iso === derived.nextRestScheduledISO);
      const nominalOnly =
        !done &&
        !effectiveRest &&
        derived.nominalRestDays &&
        derived.nominalRestDays.has(iso);
      if (effectiveRest) cell.classList.add("rest-effective");
      else if (nominalOnly) cell.classList.add("rest-nominal");
      let cellQuickTimer = null;
      cell.addEventListener("click", () => {
        if (beforeStart) return;
        if (isWorknight(h)) {
          if (cellQuickTimer) {
            clearTimeout(cellQuickTimer);
            cellQuickTimer = null;
            if (!h.cheat_days) h.cheat_days = {};
            if (h.cheat_days[iso]) {
              delete h.cheat_days[iso];
            } else if (cheatCountRolling30Ending(h, iso) >= 2) {
              setStorageStatus(false, "Worknight cheat freezes: max 2 per rolling 30 days.");
            } else {
              h.cheat_days[iso] = true;
            }
            save();
            render();
            return;
          }
          cellQuickTimer = setTimeout(() => {
            cellQuickTimer = null;
            const turningOn = !h.days[iso];
            const check = canToggleDay(h, iso, deriveHabit(h), turningOn);
            if (!check.ok) return;
            if (h.days[iso]) delete h.days[iso];
            else h.days[iso] = true;
            save();
            render();
          }, 300);
          return;
        }
        const turningOn = !h.days[iso];
        const check = canToggleDay(h, iso, deriveHabit(h), turningOn);
        if (!check.ok) return;
        if (h.days[iso]) {
          delete h.days[iso];
        } else {
          h.days[iso] = true;
        }
        save();
        render();
      });
      root.appendChild(cell);
    }

    for (let i = 0; i < startDow; i++) {
      const dayNum = prevMonthDays - startDow + i + 1;
      const iso = ymd(prevYear, prevMonth1, dayNum);
      appendDayCell(iso, dayNum, true);
    }

    for (let d = 1; d <= daysInMonth; d++) {
      const iso = ymd(viewYear, viewMonth + 1, d);
      appendDayCell(iso, d, false);
    }

    const totalCells = startDow + daysInMonth;
    const trailing = (7 - (totalCells % 7)) % 7;
    const nextFirst = new Date(viewYear, viewMonth + 1, 1);
    const nextY = nextFirst.getFullYear();
    const nextM = nextFirst.getMonth() + 1;
    for (let i = 0; i < trailing; i++) {
      const dayNum = i + 1;
      const iso = ymd(nextY, nextM, dayNum);
      appendDayCell(iso, dayNum, true);
    }
  }

  function render() {
    renderList();
    renderDetail();
  }

  document.getElementById("add-habit").addEventListener("click", () => {
    const inp = document.getElementById("title-input");
    const title = inp.value.trim();
    if (!title) return;
    const startEl = document.getElementById("start-input");
    const startVal = (startEl.value && startEl.value.trim()) || todayISO();
    const h = { id: uuid(), title, start: startVal, days: {}, habit_type: "default", cheat_days: {} };
    habits.push(h);
    selectedId = h.id;
    inp.value = "";
    const t = new Date();
    viewYear = t.getFullYear();
    viewMonth = t.getMonth();
    save();
    render();
  });

  document.getElementById("delete-habit").addEventListener("click", () => {
    if (!selectedId) return;
    habits = habits.filter((x) => x.id !== selectedId);
    selectedId = habits[0]?.id ?? null;
    save();
    render();
  });

  document.getElementById("cal-prev").addEventListener("click", () => {
    viewMonth--;
    if (viewMonth < 0) {
      viewMonth = 11;
      viewYear--;
    }
    renderDetail();
  });
  document.getElementById("cal-next").addEventListener("click", () => {
    viewMonth++;
    if (viewMonth > 11) {
      viewMonth = 0;
      viewYear++;
    }
    renderDetail();
  });

  document.getElementById("detail-start-input").addEventListener("change", () => {
    const h = habits.find((x) => x.id === selectedId);
    if (!h) return;
    const v = document.getElementById("detail-start-input").value;
    if (!v || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return;
    h.start = v;
    stripMarksBeforeStart(h);
    save();
    render();
  });

  document.getElementById("title-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("add-habit").click();
  });

  window.addEventListener("pagehide", flushBackupSync);
  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushBackupSync();
  });

  (async function init() {
    const startField = document.getElementById("start-input");
    if (startField) startField.value = todayISO();
    try {
      await loadInitialState();
      setStorageStatus(
        true,
        habits.length === 0
          ? "No habits yet — changes save to IndexedDB + localStorage backup on every action."
          : `${habits.length} habit(s) · auto-saved to IndexedDB and localStorage on each change.`,
      );
    } catch (e) {
      console.error("habit-builder: load failed", e);
      setStorageStatus(false, "Could not open local database. Check that IndexedDB is allowed for this page.");
    }
    render();
  })();
})();
