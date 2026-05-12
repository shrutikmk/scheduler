/**
 * Archived from app/day_scheduler.html — chat FIFO queue (removed 2026-05-09).
 * Depends on: promptEl, statusEl, llmOnline, runChatTurn, refreshComposeStatus,
 *   chat-queue element in HTML, and queue CSS.
 */

  /**
   * FIFO messages waiting to be sent. Each item is removed from this list as
   * soon as it is handed to ``runChatTurn`` (the user bubble is the sent copy).
   *
   * @type {{ id: string, text: string }[]}
   */
  const pendingItems = [];
  let queueIdSeq = 1;

  function newQueueId() {
    queueIdSeq += 1;
    return "q" + queueIdSeq;
  }

  /** @type {Promise<void> | null} */
  let queueDriver = null;

  // --------- Queue rendering (aesthetic preserved) -------------------------
  const queuePanelEl = document.getElementById("chat-queue");

  function svgIcon(paths) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    for (const d of paths) {
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    }
    return svg;
  }

  function makeQueueAction(act, label, paths, danger) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "queue-act" + (danger ? " danger" : "");
    btn.dataset.act = act;
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.appendChild(svgIcon(paths));
    return btn;
  }

  function buildQueueRow(item, opts) {
    const inFlight = !!(opts && opts.inFlight);
    const li = document.createElement("li");
    li.className = "queue-row" + (inFlight ? " in-flight" : "");
    li.dataset.qid = item.id;
    li.setAttribute("role", "listitem");

    const bullet = document.createElement("span");
    bullet.className = "queue-bullet";
    bullet.setAttribute("aria-hidden", "true");
    li.appendChild(bullet);

    const text = document.createElement("span");
    text.className = "queue-text";
    const oneLine = String(item.text || "").replace(/\s+/g, " ").trim();
    text.textContent = oneLine;
    text.title = oneLine;
    li.appendChild(text);

    const actions = document.createElement("span");
    actions.className = "queue-actions";
    if (!inFlight) {
      actions.appendChild(
        makeQueueAction("edit", "Edit message", [
          "M4 20h4l10-10-4-4L4 16v4z",
          "M14 6l4 4",
        ]),
      );
      actions.appendChild(
        makeQueueAction("up", "Send next", [
          "M12 19V5",
          "M5 12l7-7 7 7",
        ]),
      );
      actions.appendChild(
        makeQueueAction(
          "trash",
          "Remove",
          ["M3 6h18", "M8 6V4h8v2", "M6 6l1 14h10l1-14"],
          true,
        ),
      );
    } else {
      const tag = document.createElement("span");
      tag.className = "queue-act";
      tag.style.pointerEvents = "none";
      tag.style.fontSize = "0.7rem";
      tag.textContent = "sending";
      actions.appendChild(tag);
    }
    li.appendChild(actions);
    return li;
  }

  function renderQueuePanel() {
    if (!queuePanelEl) {
      console.warn("[queue] panel element missing");
      return;
    }
    try {
      const total = pendingItems.length;
      while (queuePanelEl.firstChild) queuePanelEl.removeChild(queuePanelEl.firstChild);
      if (total === 0) {
        queuePanelEl.hidden = true;
        queuePanelEl.style.display = "none";
        return;
      }
      queuePanelEl.removeAttribute("hidden");
      queuePanelEl.style.display = "flex";
      if (queuePanelEl.dataset.collapsed !== "true") {
        queuePanelEl.dataset.collapsed = "false";
      }
      const collapsed = queuePanelEl.dataset.collapsed === "true";

      const head = document.createElement("div");
      head.className = "queue-head";
      head.setAttribute("role", "button");
      head.tabIndex = 0;
      head.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const twisty = document.createElement("span");
      twisty.className = "queue-twisty";
      twisty.textContent = collapsed ? "▸" : "▾";
      head.appendChild(twisty);
      const count = document.createElement("span");
      count.className = "queue-count";
      count.textContent = total + " Queued";
      head.appendChild(count);
      queuePanelEl.appendChild(head);

      const ul = document.createElement("ul");
      ul.className = "queue-list";
      for (const item of pendingItems) ul.appendChild(buildQueueRow(item));
      queuePanelEl.appendChild(ul);
    } catch (err) {
      console.error("[queue] render failed:", err);
    }
  }

  function findQueueIndex(qid) {
    for (let i = 0; i < pendingItems.length; i += 1) {
      if (pendingItems[i].id === qid) return i;
    }
    return -1;
  }

  function enqueueUserText(text) {
    const trimmed = String(text || "").trim();
    if (!trimmed) return null;
    const item = { id: newQueueId(), text: trimmed };
    pendingItems.push(item);
    renderQueuePanel();
    refreshComposeStatus();
    return item;
  }

  function clearPendingQueue() {
    pendingItems.length = 0;
    renderQueuePanel();
    refreshComposeStatus();
  }

  function trashQueuedMessage(qid) {
    const i = findQueueIndex(qid);
    if (i < 0) return;
    pendingItems.splice(i, 1);
    renderQueuePanel();
    refreshComposeStatus();
  }

  function promoteQueuedMessage(qid) {
    const i = findQueueIndex(qid);
    if (i <= 0) return;
    const [item] = pendingItems.splice(i, 1);
    pendingItems.unshift(item);
    renderQueuePanel();
  }

  function editQueuedMessage(qid) {
    const i = findQueueIndex(qid);
    if (i < 0) return;
    const [item] = pendingItems.splice(i, 1);
    const stash = promptEl.value.trim();
    if (stash) {
      const stashed = { id: newQueueId(), text: stash };
      pendingItems.unshift(stashed);
    }
    promptEl.value = item.text;
    promptEl.focus();
    try {
      const len = promptEl.value.length;
      promptEl.setSelectionRange(len, len);
    } catch (_) {}
    renderQueuePanel();
    refreshComposeStatus();
  }

  /** One serial driver: next message starts only after ``runChatTurn`` fully finishes. */
  function scheduleQueueWork() {
    if (queueDriver != null) return;
    if (!llmOnline || pendingItems.length === 0) return;
    queueDriver = (async function () {
      try {
        while (pendingItems.length > 0 && llmOnline) {
          const next = pendingItems.shift();
          if (!next) continue;
          renderQueuePanel();
          refreshComposeStatus();
          try {
            await runChatTurn(next.text);
          } catch (err) {
            console.error("runChatTurn rejected", err);
            statusEl.textContent =
              "Send failed: " + String(err && err.message ? err.message : err);
          } finally {
            renderQueuePanel();
            refreshComposeStatus();
          }
        }
      } finally {
        queueDriver = null;
        if (pendingItems.length > 0 && llmOnline) {
          scheduleQueueWork();
        }
      }
    })();
  }

  if (queuePanelEl) {
    queuePanelEl.addEventListener("click", function (ev) {
      const head = ev.target.closest(".queue-head");
      if (head && queuePanelEl.contains(head)) {
        queuePanelEl.dataset.collapsed =
          queuePanelEl.dataset.collapsed === "true" ? "false" : "true";
        renderQueuePanel();
        return;
      }
      const btn = ev.target.closest(".queue-act");
      if (!btn) return;
      const row = btn.closest(".queue-row");
      if (!row) return;
      const qid = row.dataset.qid;
      const act = btn.dataset.act;
      if (act === "edit") editQueuedMessage(qid);
      else if (act === "up") promoteQueuedMessage(qid);
      else if (act === "trash") trashQueuedMessage(qid);
    });
    queuePanelEl.addEventListener("keydown", function (ev) {
      const head = ev.target.closest(".queue-head");
      if (!head) return;
      if (ev.key !== "Enter" && ev.key !== " ") return;
      ev.preventDefault();
      queuePanelEl.dataset.collapsed =
        queuePanelEl.dataset.collapsed === "true" ? "false" : "true";
      renderQueuePanel();
    });
  }
