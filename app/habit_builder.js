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
    if (!id) return null;
    return { id, title, start, days };
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

  /** @type {{ id: string, title: string, start: string, days: Record<string, boolean> }[]} */
  let habits = [];
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
  }

  function uuid() {
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  /** Marks → sorted ISO date strings */
  function markedDates(h) {
    return Object.keys(h.days || {}).filter((k) => h.days[k]).sort();
  }

  /** Furthest date we need for phase-2 simulation: today or latest mark, whichever is later (ISO date order). */
  function maxDerivedScanISO(h) {
    const marks = markedDates(h);
    const t = todayISO();
    if (!marks.length) return t;
    const last = marks[marks.length - 1];
    return last > t ? last : t;
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
    while (daysDiff(day, maxScanISO) <= 0 && !complete && guard < 12000) {
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
   * Re-run simulation tracking rest-window for UI.
   */
  function deriveRestDaySet(boundaryISO, marksSet) {
    const set = new Set();
    const mark = new Set(marksSet);
    let day = addDaysISO(boundaryISO, 1);
    let targetL = 8;
    let run = 0;
    let needRest = false;
    let complete = false;
    const maxScanISO = addDaysISO(todayISO(), 365 * 5);
    let guard = 0;
    while (daysDiff(day, maxScanISO) <= 0 && !complete && guard < 12000) {
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

  function formatScheduleDay(iso) {
    return parseISODate(iso).toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    });
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

  function nextRestFromSet(restDays, todayISO) {
    let best = null;
    for (const r of restDays) {
      if (daysDiff(todayISO, r) < 0) continue;
      if (best === null || r < best) best = r;
    }
    return best;
  }

  function nextHabitDayPhase2(boundaryISO, marksSet, todayISO, programComplete) {
    if (programComplete) return null;
    const mark = marksSet instanceof Set ? marksSet : new Set(marksSet);
    const firstP2 = addDaysISO(boundaryISO, 1);
    if (daysDiff(todayISO, firstP2) > 0) return firstP2;

    let d = todayISO;
    for (let i = 0; i < 800; i++) {
      const st = stateAtStartOfDay(boundaryISO, mark, d);
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
    const marks = markedDates(h);
    const marksSet = new Set(marks);
    const { actual, phase1Earned, phase1Satisfied } = phase1Stats(h.start, marks);

    const boundary = phase1Satisfied ? phase2BoundaryDate(h.start, marks) : null;
    const maxISO = maxDerivedScanISO(h);
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
      forgivingPct = (100 * (phase1Earned + forgivingPhase2Pts)) / TOTAL_POINTS;
      if (forgivingComplete) forgivingPct = 100;

      restDays = deriveRestDaySet(boundary, marksSet);
    }

    const programDone = Boolean(strictComplete || forgivingComplete);

    const today = todayISO();
    let status = "";
    if (!phase1Satisfied) {
      let rawW = weekIndexForDate(h.start, today);
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
      const r = sim.needRest ? "log a rest day (leave today unchecked)" : `streak ${sim.run}/${sim.targetL}`;
      status =
        `Long run: streak target ${sim.targetL}; ${r}. Progress bar uses your latest contiguous streak.`;
    }

    let nextRestLabel = "—";
    let nextHabitLabel = "—";

    if (daysDiff(today, h.start) > 0) {
      nextHabitLabel = `${formatScheduleDay(h.start)} (habit hasn’t started)`;
      nextRestLabel = "— (streak rests start in phase 2)";
    } else if (!phase1Satisfied) {
      const nh = nextHabitDayPhase1(h, actual, h.start, today);
      nextRestLabel = "— (phase 1 has no rest days)";
      if (nh) {
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
      const nr = nextRestFromSet(restDays, today);
      nextRestLabel = nr ? `${formatScheduleDay(nr)} (leave unchecked)` : "—";
      const nh = nextHabitDayPhase2(boundary, marksSet, today, programDone);
      if (nh) {
        nextHabitLabel =
          daysDiff(today, nh) === 0 ? `${formatScheduleDay(nh)} (today)` : formatScheduleDay(nh);
      } else {
        nextHabitLabel = "—";
      }
    }

    const pct = Math.min(100, Math.max(0, forgivingPct));

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
    };
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
      const titleEl = document.createElement("span");
      titleEl.className = "habit-title";
      titleEl.textContent = h.title || "(untitled)";
      const meta = document.createElement("span");
      meta.className = "muted habit-li-meta";
      const n = markedDates(h).length;
      meta.textContent = `Start ${h.start} · ${n} day(s) logged`;
      left.appendChild(titleEl);
      left.appendChild(meta);
      li.appendChild(left);
      const d = deriveHabit(h);
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
    detailStart.title =
      "Phase 1 weeks are Sun–Sat; week 1 is the calendar week that contains this date. Your first log must be this date or later.";
    const derived = deriveHabit(h);
    document.getElementById("detail-status").textContent = derived.status;

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
    addPtsRow("Phase 1", `${derived.phase1Earned} / 28 pts`);
    addPtsRow("Phase 2 · Progress track", `${derived.forgivingPhase2Pts} / ${PHASE2_MAX_POINTS} pts`);
    addPtsRow("Phase 2 · Long-run track", `${derived.phase2Earned} / ${PHASE2_MAX_POINTS} pts`);

    const upcoming = document.getElementById("detail-upcoming");
    upcoming.hidden = false;
    upcoming.replaceChildren();
    const rowRest = document.createElement("div");
    const sr = document.createElement("strong");
    sr.textContent = "Next rest day";
    rowRest.appendChild(sr);
    rowRest.appendChild(document.createTextNode(` · ${derived.nextRestLabel}`));
    upcoming.appendChild(rowRest);
    const rowHabit = document.createElement("div");
    const sh = document.createElement("strong");
    sh.textContent = "Next day to log habit (latest deadline)";
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
      if (derived.restDays.has(iso) && !done) cell.classList.add("rest-block");
      cell.addEventListener("click", () => {
        if (beforeStart) return;
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
    const h = { id: uuid(), title, start: startVal, days: {} };
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
