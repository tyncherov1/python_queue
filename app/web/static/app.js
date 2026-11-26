(() => {
  "use strict";

  const RING_CIRCUMFERENCE = 2 * Math.PI * 142; // r=142 in index.html SVG

  // Local UI preferences persisted in localStorage
  const prefs = {
    soundEnabled: localStorage.getItem("sq_sound") !== "0",
    alarmStyle: localStorage.getItem("sq_alarm_style") || "bell",
    volume: Number(localStorage.getItem("sq_volume") || "85"),
    oneMinWarn: localStorage.getItem("sq_one_min_warn") !== "0",
    pin: localStorage.getItem("sq_pin") || "",
  };

  // Runtime state synced from server
  let boardState = null;
  let serverTimeOffsetMs = 0;
  let ws = null;
  let wsReconnectDelay = 1000;
  let wsPingTimer = null;
  let pinVerified = false;
  let editMode = false;
  let showAllQueue = false;
  let rosterCache = [];

  // Alarm tracking so each turn/epoch rings exactly once when crossing 00:00
  let alarmRangEpoch = null;
  let oneMinWarnEpoch = null;
  let lastRenderedRemaining = null;
  let audioCtx = null;

  // DOM references
  const el = {
    connBadge: document.getElementById("connBadge"),
    connLabel: document.getElementById("connLabel"),
    seminarTitle: document.getElementById("seminarTitle"),
    queueOpenBadge: document.getElementById("queueOpenBadge"),
    wallClock: document.getElementById("wallClock"),
    btnSoundToggle: document.getElementById("btnSoundToggle"),
    soundIcon: document.getElementById("soundIcon"),
    btnTestBell: document.getElementById("btnTestBell"),
    btnPinLock: document.getElementById("btnPinLock"),
    pinLockIcon: document.getElementById("pinLockIcon"),
    btnSettings: document.getElementById("btnSettings"),
    btnFullscreen: document.getElementById("btnFullscreen"),

    speakerBadge: document.getElementById("speakerBadge"),
    speakerMeta: document.getElementById("speakerMeta"),
    currentStudentName: document.getElementById("currentStudentName"),
    timerProgress: document.getElementById("timerProgress"),
    timerDisplay: document.getElementById("timerDisplay"),
    timerSublabel: document.getElementById("timerSublabel"),
    alarmFlash: document.getElementById("alarmFlash"),

    controlsPanel: document.getElementById("controlsPanel"),
    lockedBanner: document.getElementById("lockedBanner"),
    btnUnlockFromBanner: document.getElementById("btnUnlockFromBanner"),
    btnStartPause: document.getElementById("btnStartPause"),
    startPauseIcon: document.getElementById("startPauseIcon"),
    startPauseText: document.getElementById("startPauseText"),
    btnNext: document.getElementById("btnNext"),
    nextBtnText: document.getElementById("nextBtnText"),
    btnAddMinute: document.getElementById("btnAddMinute"),
    btnResetTimer: document.getElementById("btnResetTimer"),

    statServed: document.getElementById("statServed"),
    statAvg: document.getElementById("statAvg"),
    statRoster: document.getElementById("statRoster"),

    queueCountPill: document.getElementById("queueCountPill"),
    queueEtaPill: document.getElementById("queueEtaPill"),
    btnToggleEdit: document.getElementById("btnToggleEdit"),
    boardEditBar: document.getElementById("boardEditBar"),
    btnOpenAddModal: document.getElementById("btnOpenAddModal"),
    queueList: document.getElementById("queueList"),
    btnShowMore: document.getElementById("btnShowMore"),
    qrCard: document.getElementById("qrCard"),
    qrImage: document.getElementById("qrImage"),
    qrBotHandle: document.getElementById("qrBotHandle"),
    demoCard: document.getElementById("demoCard"),
    btnDemoAdd: document.getElementById("btnDemoAdd"),

    settingsModal: document.getElementById("settingsModal"),
    inputTimerMin: document.getElementById("inputTimerMin"),
    inputCooldownMin: document.getElementById("inputCooldownMin"),
    inputMaxSlots: document.getElementById("inputMaxSlots"),
    inputVisibleCount: document.getElementById("inputVisibleCount"),
    inputSeminarTitle: document.getElementById("inputSeminarTitle"),
    toggleQueueOpen: document.getElementById("toggleQueueOpen"),
    toggleAutoStart: document.getElementById("toggleAutoStart"),
    toggleOneMinWarn: document.getElementById("toggleOneMinWarn"),
    selectAlarmSound: document.getElementById("selectAlarmSound"),
    inputVolume: document.getElementById("inputVolume"),
    volumeVal: document.getElementById("volumeVal"),
    timerPresetChips: document.getElementById("timerPresetChips"),
    btnModalTestBell: document.getElementById("btnModalTestBell"),
    btnUndoCall: document.getElementById("btnUndoCall"),
    btnExportCsv: document.getElementById("btnExportCsv"),
    btnClearQueue: document.getElementById("btnClearQueue"),
    btnSaveSettings: document.getElementById("btnSaveSettings"),

    addStudentModal: document.getElementById("addStudentModal"),
    selectRosterStudent: document.getElementById("selectRosterStudent"),
    inputCustomStudent: document.getElementById("inputCustomStudent"),
    inputInsertPos: document.getElementById("inputInsertPos"),
    btnConfirmAddStudent: document.getElementById("btnConfirmAddStudent"),

    pinModal: document.getElementById("pinModal"),
    inputPin: document.getElementById("inputPin"),
    pinError: document.getElementById("pinError"),
    btnSubmitPin: document.getElementById("btnSubmitPin"),

    toastContainer: document.getElementById("toastContainer"),
  };

  // =========================================================================
  // Web Audio API Bell & Chime Synthesizer (no external mp3 files needed!)
  // =========================================================================
  function ensureAudio() {
    if (!audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) {
        audioCtx = new Ctx();
      }
    }
    if (audioCtx && audioCtx.state === "suspended") {
      audioCtx.resume().catch(() => {});
    }
    return audioCtx;
  }

  function playBellStrike(ctx, startTime, baseFreq, gainLevel, duration) {
    const harmonics = [
      { mult: 1.0, amp: 1.0, decay: duration },
      { mult: 2.0, amp: 0.45, decay: duration * 0.72 },
      { mult: 2.756, amp: 0.28, decay: duration * 0.55 },
      { mult: 4.07, amp: 0.14, decay: duration * 0.4 },
    ];
    harmonics.forEach(({ mult, amp, decay }) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(baseFreq * mult, startTime);

      gain.gain.setValueAtTime(0.0001, startTime);
      gain.gain.exponentialRampToValueAtTime(Math.max(0.001, gainLevel * amp), startTime + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, startTime + decay);

      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(startTime);
      osc.stop(startTime + decay + 0.05);
    });
  }

  function playAlarmSound(style = prefs.alarmStyle, force = false) {
    if (!force && !prefs.soundEnabled) return;
    const ctx = ensureAudio();
    if (!ctx) return;

    const vol = Math.max(0.05, Math.min(1, prefs.volume / 100)) * 0.55;
    const now = ctx.currentTime + 0.02;

    if (style === "bell") {
      // Classroom brass desk bell — 3 bright resonant rings
      playBellStrike(ctx, now, 1046.5, vol, 1.4);
      playBellStrike(ctx, now + 0.42, 1046.5, vol, 1.4);
      playBellStrike(ctx, now + 0.84, 1318.5, vol * 1.1, 1.8);
    } else if (style === "chime") {
      // Warm ascending major chord chime (C5 -> E5 -> G5 -> C6)
      [523.25, 659.25, 783.99, 1046.5].forEach((freq, idx) => {
        playBellStrike(ctx, now + idx * 0.2, freq, vol * 0.9, 1.8);
      });
    } else {
      // Crisp triple electronic alert
      [0, 0.28, 0.56, 0.95].forEach((offset, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "triangle";
        osc.frequency.setValueAtTime(idx === 3 ? 1174.66 : 880, now + offset);
        const dur = idx === 3 ? 0.55 : 0.18;
        gain.gain.setValueAtTime(0.001, now + offset);
        gain.gain.exponentialRampToValueAtTime(vol, now + offset + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.001, now + offset + dur);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now + offset);
        osc.stop(now + offset + dur + 0.03);
      });
    }
  }

  function playWarningPing() {
    if (!prefs.soundEnabled || !prefs.oneMinWarn) return;
    const ctx = ensureAudio();
    if (!ctx) return;
    const vol = Math.max(0.04, Math.min(1, prefs.volume / 100)) * 0.25;
    playBellStrike(ctx, ctx.currentTime + 0.02, 783.99, vol, 0.75);
  }

  function triggerVisualFlash() {
    el.alarmFlash.classList.remove("active");
    void el.alarmFlash.offsetWidth;
    el.alarmFlash.classList.add("active");
  }

  // =========================================================================
  // Helpers & API Client
  // =========================================================================
  function showToast(text) {
    const node = document.createElement("div");
    node.className = "toast";
    node.textContent = text;
    el.toastContainer.appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "opacity 0.25s ease";
      setTimeout(() => node.remove(), 260);
    }, 2600);
  }

  function isControlsUnlocked() {
    if (!boardState || !boardState.pin_required) return true;
    return pinVerified;
  }

  async function apiRequest(path, method = "POST", body = null) {
    const headers = {};
    if (prefs.pin) {
      headers["X-Board-Pin"] = prefs.pin;
    }
    if (body !== null) {
      headers["Content-Type"] = "application/json";
    }
    const resp = await fetch(path, {
      method,
      headers,
      body: body !== null ? JSON.stringify(body) : undefined,
    });
    if (resp.status === 403) {
      pinVerified = false;
      updateLockUI();
      openPinModal();
      throw new Error("Требуется PIN-код преподавателя");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || "Ошибка запроса");
    }
    return data;
  }

  function formatMMSS(secFloat) {
    const isOver = secFloat < 0;
    const absSec = isOver ? Math.floor(Math.abs(secFloat)) : Math.ceil(secFloat);
    const m = Math.floor(absSec / 60);
    const s = absSec % 60;
    const prefix = isOver ? "+" : "";
    return `${prefix}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function formatAvgDuration(sec) {
    if (!sec || sec <= 0) return "—";
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function computeEffectiveRemaining(timer) {
    if (!timer) return 420;
    if (timer.status === "running" && timer.started_at != null) {
      const syncedNowSec = (Date.now() + serverTimeOffsetMs) / 1000;
      return timer.stored_remaining_sec - (syncedNowSec - timer.started_at);
    }
    return timer.stored_remaining_sec;
  }

  // =========================================================================
  // Render State & Timer Loop
  // =========================================================================
  function updateSoundUI() {
    el.soundIcon.textContent = prefs.soundEnabled ? "🔔" : "🔕";
    el.btnSoundToggle.classList.toggle("muted", !prefs.soundEnabled);
  }

  function updateLockUI() {
    const pinRequired = Boolean(boardState && boardState.pin_required);
    el.btnPinLock.classList.toggle("hidden", !pinRequired);
    const unlocked = isControlsUnlocked();
    el.pinLockIcon.textContent = unlocked ? "🔓" : "🔒";
    el.controlsPanel.classList.toggle("hidden", !unlocked);
    el.lockedBanner.classList.toggle("hidden", unlocked);
    el.btnToggleEdit.classList.toggle("hidden", !unlocked);
    if (!unlocked) {
      editMode = false;
      el.boardEditBar.classList.add("hidden");
      el.btnToggleEdit.classList.remove("active");
    }
  }

  function renderTimerTick() {
    if (!boardState) return;
    const timer = boardState.timer;
    const duration = Math.max(10, timer.duration_sec || 420);
    const remaining = computeEffectiveRemaining(timer);

    // Detect crossing 1:00 and 0:00 while running
    if (timer.status === "running") {
      if (
        lastRenderedRemaining !== null &&
        lastRenderedRemaining > 60 &&
        remaining <= 60 &&
        remaining > 0 &&
        duration > 90 &&
        oneMinWarnEpoch !== timer.epoch
      ) {
        oneMinWarnEpoch = timer.epoch;
        playWarningPing();
        showToast("⏳ Осталась 1 минута");
      }

      if (
        remaining <= 0 &&
        alarmRangEpoch !== timer.epoch &&
        (lastRenderedRemaining === null || lastRenderedRemaining > 0 || remaining > -2.5)
      ) {
        alarmRangEpoch = timer.epoch;
        playAlarmSound();
        triggerVisualFlash();
        showToast("🔔 Время вышло!");
      }
    }
    lastRenderedRemaining = remaining;

    // Determine visual phase
    let phase = "idle";
    if (timer.status === "running") {
      if (remaining <= 0) phase = "overtime";
      else if (remaining <= 60) phase = "warning";
      else phase = "running";
    } else if (timer.status === "paused") {
      phase = remaining <= 0 ? "overtime" : "paused";
    }

    document.body.setAttribute("data-timer-phase", phase);
    const formatted = formatMMSS(remaining);
    el.timerDisplay.textContent = formatted;

    // Live countdown in browser tab title when timer is active
    const baseTitle = boardState.seminar_title || "Семинар • Очередь";
    if (timer.status === "running" && boardState.current) {
      const icon = phase === "overtime" ? "🔔" : "⏱";
      document.title = `${icon} ${formatted} — ${boardState.current.display_name}`;
    } else if (timer.status === "paused" && boardState.current) {
      document.title = `⏸ ${formatted} — ${boardState.current.display_name}`;
    } else {
      document.title = baseTitle;
    }

    // Update radial SVG progress ring
    const ratio = Math.max(0, Math.min(1, remaining / duration));
    const offset = remaining <= 0 ? 0 : RING_CIRCUMFERENCE * (1 - ratio);
    el.timerProgress.style.strokeDasharray = `${RING_CIRCUMFERENCE}`;
    el.timerProgress.style.strokeDashoffset = `${offset}`;

    const hasCurrent = Boolean(boardState.current);
    const hasWaiting = (boardState.queue || []).length > 0;

    // Sublabel & speaker badge overtime state
    if (!hasCurrent) {
      el.timerSublabel.textContent = hasWaiting ? "Вызовите первого из очереди" : "Очередь пуста";
    } else if (phase === "overtime") {
      el.timerSublabel.textContent = "ВРЕМЯ ВЫШЛО • переработка";
    } else if (phase === "warning") {
      el.timerSublabel.textContent = "Осталось меньше минуты";
    } else if (phase === "running") {
      el.timerSublabel.textContent = "Идёт отсчёт";
    } else if (phase === "paused") {
      el.timerSublabel.textContent = "На паузе";
    } else {
      el.timerSublabel.textContent = "Нажмите «Старт»";
    }

    el.speakerBadge.className = "speaker-badge " + (
      phase === "overtime" ? "overtime" : hasCurrent ? "active" : "idle"
    );
    el.speakerBadge.textContent =
      phase === "overtime"
        ? "🔔 ВРЕМЯ ВЫШЛО"
        : hasCurrent
          ? "СЕЙЧАС ОТВЕЧАЕТ"
          : "ОЖИДАНИЕ";

    // Start / Pause button appearance
    if (timer.status === "running") {
      el.btnStartPause.classList.add("is-running");
      el.startPauseIcon.textContent = "⏸";
      el.startPauseText.textContent = "Пауза";
    } else if (timer.status === "paused") {
      el.btnStartPause.classList.remove("is-running");
      el.startPauseIcon.textContent = "▶";
      el.startPauseText.textContent = "Продолжить";
    } else {
      el.btnStartPause.classList.remove("is-running");
      el.startPauseIcon.textContent = "▶";
      el.startPauseText.textContent = "Старт";
    }
  }

  function renderQueueList(state) {
    const queue = state.queue || [];
    const visibleLimit = showAllQueue ? queue.length : (state.board_visible_queue || 7);
    const visibleItems = queue.slice(0, visibleLimit);

    el.queueCountPill.textContent = `${queue.length} чел.`;
    if (queue.length > 0) {
      const totalMin = Math.round((queue.length * (state.timer.duration_sec || 420)) / 60);
      const hrs = Math.floor(totalMin / 60);
      const remMin = totalMin % 60;
      el.queueEtaPill.textContent = hrs > 0 ? `~${hrs} ч ${remMin} м` : `~${totalMin} мин`;
      el.queueEtaPill.classList.remove("hidden");
    } else {
      el.queueEtaPill.classList.add("hidden");
    }
    el.queueList.innerHTML = "";

    if (queue.length === 0) {
      const empty = document.createElement("div");
      empty.className = "queue-empty";
      empty.innerHTML = `
        <div class="queue-empty-icon">📋</div>
        <div><strong>Очередь пока пуста</strong></div>
        <div style="font-size:0.84rem">Ученики могут записаться через Telegram-бот</div>
      `;
      el.queueList.appendChild(empty);
      el.btnShowMore.classList.add("hidden");
      return;
    }

    const unlocked = isControlsUnlocked();
    const estPerStudentMin = Math.max(1, Math.round((state.timer.duration_sec || 420) / 60));

    visibleItems.forEach((item, index) => {
      const card = document.createElement("div");
      card.className = "queue-item" + (index === 0 ? " is-first" : "");
      card.setAttribute("role", "listitem");

      const posEl = document.createElement("div");
      posEl.className = "queue-pos";
      posEl.textContent = item.position;

      const infoEl = document.createElement("div");
      infoEl.className = "queue-info";

      const nameEl = document.createElement("div");
      nameEl.className = "queue-name";
      nameEl.textContent = item.display_name;

      const subEl = document.createElement("div");
      subEl.className = "queue-sub";
      if (index === 0) {
        const tag = document.createElement("span");
        tag.className = "next-tag";
        tag.textContent = "Следующий • приготовьтесь";
        subEl.appendChild(tag);
      } else {
        const eta = document.createElement("span");
        eta.textContent = `~${index * estPerStudentMin} мин`;
        subEl.appendChild(eta);
      }

      infoEl.appendChild(nameEl);
      infoEl.appendChild(subEl);
      card.appendChild(posEl);
      card.appendChild(infoEl);

      if (editMode && unlocked) {
        const btns = document.createElement("div");
        btns.className = "queue-edit-btns";

        if (item.position > 1) {
          const upBtn = document.createElement("button");
          upBtn.className = "mini-btn";
          upBtn.type = "button";
          upBtn.title = "Поднять выше";
          upBtn.textContent = "▲";
          upBtn.addEventListener("click", () => moveEntry(item.id, item.position - 1));
          btns.appendChild(upBtn);
        }

        if (item.position < queue.length) {
          const downBtn = document.createElement("button");
          downBtn.className = "mini-btn";
          downBtn.type = "button";
          downBtn.title = "Опустить ниже";
          downBtn.textContent = "▼";
          downBtn.addEventListener("click", () => moveEntry(item.id, item.position + 1));
          btns.appendChild(downBtn);
        }

        const delBtn = document.createElement("button");
        delBtn.className = "mini-btn del";
        delBtn.type = "button";
        delBtn.title = "Удалить из очереди";
        delBtn.textContent = "✕";
        delBtn.addEventListener("click", () => deleteEntry(item.id, item.display_name));
        btns.appendChild(delBtn);

        card.appendChild(btns);
      }

      el.queueList.appendChild(card);
    });

    const hiddenCount = queue.length - visibleItems.length;
    if (hiddenCount > 0) {
      el.btnShowMore.classList.remove("hidden");
      el.btnShowMore.textContent = `Показать ещё ${hiddenCount} чел. из очереди`;
    } else if (showAllQueue && queue.length > (state.board_visible_queue || 7)) {
      el.btnShowMore.classList.remove("hidden");
      el.btnShowMore.textContent = "Свернуть список";
    } else {
      el.btnShowMore.classList.add("hidden");
    }
  }

  function applyState(state) {
    const prevEpoch = boardState?.timer?.epoch;
    boardState = state;
    if (state.server_now) {
      serverTimeOffsetMs = state.server_now * 1000 - Date.now();
    }
    if (prevEpoch !== undefined && state.timer.epoch !== prevEpoch) {
      lastRenderedRemaining = state.timer.remaining_sec;
    }

    // Header & title
    el.seminarTitle.textContent = state.seminar_title || "Семинар • Очередь к преподавателю";
    document.title = state.seminar_title || "Семинар • Очередь к преподавателю";
    el.queueOpenBadge.textContent = state.queue_open ? "Запись открыта" : "Запись закрыта";
    el.queueOpenBadge.className = "status-pill " + (state.queue_open ? "open" : "closed");

    // Current student
    const hasWaiting = (state.queue || []).length > 0;
    if (state.current) {
      el.currentStudentName.textContent = state.current.display_name;
      el.currentStudentName.classList.remove("empty");
      el.speakerMeta.textContent =
        state.current.visits_count > 0
          ? `Подход #${state.current.visits_count + 1}`
          : "";
      el.nextBtnText.textContent = "Следующий человек";
    } else {
      el.currentStudentName.textContent = hasWaiting
        ? "Нажмите «Вызвать первого», чтобы начать"
        : "Очередь пуста — ожидаем запись в Telegram";
      el.currentStudentName.classList.add("empty");
      el.speakerMeta.textContent = "";
      el.nextBtnText.textContent = hasWaiting ? "Вызвать первого" : "Следующий человек";
    }

    // Timer controls are only available while someone is actively called to the teacher
    const canRunTimer = Boolean(state.current);
    el.btnStartPause.disabled = !canRunTimer;
    el.btnAddMinute.disabled = !canRunTimer;
    el.btnResetTimer.disabled = !canRunTimer;
    el.btnNext.disabled = !state.current && !hasWaiting;

    // Stats
    const stats = state.stats || {};
    el.statServed.textContent = String(stats.served_count ?? 0);
    el.statAvg.textContent = formatAvgDuration(stats.avg_duration_sec || 0);
    el.statRoster.textContent = String(stats.roster_count ?? 0);

    // QR Code vs Demo Helper
    if (state.bot_username) {
      el.qrCard.classList.remove("hidden");
      el.demoCard.classList.add("hidden");
      el.qrBotHandle.textContent = `@${state.bot_username}`;
      const expectedSrc = `/api/qr.svg?u=${encodeURIComponent(state.bot_username)}`;
      if (!el.qrImage.src.endsWith(expectedSrc)) {
        el.qrImage.src = expectedSrc;
      }
    } else {
      el.qrCard.classList.add("hidden");
      el.demoCard.classList.toggle("hidden", !isControlsUnlocked());
    }

    updateLockUI();
    renderQueueList(state);
    renderTimerTick();
  }

  // =========================================================================
  // WebSocket Real-Time Connection
  // =========================================================================
  function setConnStatus(mode, label) {
    el.connBadge.className = `conn-badge ${mode}`;
    el.connLabel.textContent = label;
  }

  function connectWebSocket() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws`;
    setConnStatus("connecting", "Подключение...");

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      wsReconnectDelay = 1000;
      setConnStatus("online", "Онлайн");
      if (wsPingTimer) clearInterval(wsPingTimer);
      wsPingTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send("ping");
        }
      }, 25000);
    };

    ws.onmessage = (event) => {
      if (event.data === "pong") return;
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "state" && msg.data) {
          applyState(msg.data);
        }
      } catch (err) {
        console.error("WS parse error:", err);
      }
    };

    ws.onclose = () => {
      if (wsPingTimer) clearInterval(wsPingTimer);
      setConnStatus("offline", "Переподключение...");
      setTimeout(connectWebSocket, wsReconnectDelay);
      wsReconnectDelay = Math.min(wsReconnectDelay * 1.5, 8000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  // =========================================================================
  // Board Actions
  // =========================================================================
  async function toggleStartPause() {
    ensureAudio();
    if (!boardState || !boardState.current) {
      showToast("Сначала вызовите ученика из очереди");
      return;
    }
    try {
      const endpoint =
        boardState.timer.status === "running" ? "/api/timer/pause" : "/api/timer/start";
      const state = await apiRequest(endpoint, "POST");
      applyState(state);
    } catch (err) {
      showToast(err.message);
    }
  }

  async function callNext() {
    ensureAudio();
    try {
      const res = await apiRequest("/api/queue/next", "POST");
      if (res.state) applyState(res.state);
      if (res.state?.current) {
        showToast(`⏭ Вызван(а): ${res.state.current.display_name}`);
      } else {
        showToast("Очередь завершена");
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  async function addOneMinute() {
    ensureAudio();
    if (!boardState || !boardState.current) return;
    try {
      const state = await apiRequest("/api/timer/add", "POST", { seconds: 60 });
      applyState(state);
      showToast("⏱ Добавлена +1 минута");
    } catch (err) {
      showToast(err.message);
    }
  }

  async function resetTimer() {
    ensureAudio();
    if (!boardState || !boardState.current) return;
    try {
      const state = await apiRequest("/api/timer/reset", "POST");
      applyState(state);
      showToast("↺ Таймер сброшен");
    } catch (err) {
      showToast(err.message);
    }
  }

  async function moveEntry(entryId, newPos) {
    try {
      const res = await apiRequest(`/api/queue/${entryId}/move`, "POST", {
        new_position: newPos,
      });
      if (res.state) applyState(res.state);
    } catch (err) {
      showToast(err.message);
    }
  }

  async function deleteEntry(entryId, name) {
    try {
      const res = await apiRequest(`/api/queue/${entryId}`, "DELETE");
      if (res.state) applyState(res.state);
      showToast(`🗑 Удалён(а): ${name}`);
    } catch (err) {
      showToast(err.message);
    }
  }

  async function addDemoStudent() {
    ensureAudio();
    try {
      if (rosterCache.length === 0) {
        rosterCache = await apiRequest("/api/students", "GET");
      }
      const queuedIds = new Set((boardState?.queue || []).map((q) => q.student_id));
      if (boardState?.current?.student_id) queuedIds.add(boardState.current.student_id);
      const available = rosterCache.filter((s) => !queuedIds.has(s.id));
      if (available.length === 0) {
        showToast("Все 32 ученика уже в очереди!");
        return;
      }
      const pick = available[Math.floor(Math.random() * available.length)];
      const res = await apiRequest("/api/queue/insert", "POST", { student_id: pick.id });
      if (res.state) applyState(res.state);
      showToast(`➕ ${pick.full_name} добавлен(а) в очередь`);
    } catch (err) {
      showToast(err.message);
    }
  }

  // =========================================================================
  // Modals: Settings, Add Student, PIN
  // =========================================================================
  function openSettingsModal() {
    if (!isControlsUnlocked()) {
      openPinModal();
      return;
    }
    if (!boardState) return;
    const minVal = Math.round((boardState.timer.duration_sec / 60) * 10) / 10;
    el.inputTimerMin.value = String(minVal);
    el.inputCooldownMin.value = String(boardState.rejoin_cooldown_minutes ?? 15);
    el.inputMaxSlots.value = String(boardState.max_active_slots ?? 2);
    el.inputVisibleCount.value = String(boardState.board_visible_queue ?? 7);
    el.inputSeminarTitle.value = boardState.seminar_title || "";
    el.toggleQueueOpen.checked = Boolean(boardState.queue_open);
    el.toggleAutoStart.checked = Boolean(boardState.auto_start_timer);
    el.toggleOneMinWarn.checked = prefs.oneMinWarn;
    el.selectAlarmSound.value = prefs.alarmStyle;
    el.inputVolume.value = String(prefs.volume);
    el.volumeVal.textContent = String(prefs.volume);
    highlightPresetChip(minVal);

    el.settingsModal.classList.remove("hidden");
  }

  function highlightPresetChip(minutes) {
    el.timerPresetChips.querySelectorAll(".chip").forEach((chip) => {
      chip.classList.toggle("selected", Number(chip.dataset.min) === Number(minutes));
    });
  }

  async function saveSettings() {
    const minutes = parseFloat(el.inputTimerMin.value);
    if (Number.isNaN(minutes) || minutes < 0.5 || minutes > 120) {
      showToast("Укажите корректное время от 0.5 до 120 мин");
      return;
    }

    // Save local audio prefs
    prefs.alarmStyle = el.selectAlarmSound.value;
    prefs.volume = Number(el.inputVolume.value);
    prefs.oneMinWarn = el.toggleOneMinWarn.checked;
    localStorage.setItem("sq_alarm_style", prefs.alarmStyle);
    localStorage.setItem("sq_volume", String(prefs.volume));
    localStorage.setItem("sq_one_min_warn", prefs.oneMinWarn ? "1" : "0");

    try {
      const updated = await apiRequest("/api/settings", "PATCH", {
        seminar_title: el.inputSeminarTitle.value.trim(),
        timer_duration_sec: Math.round(minutes * 60),
        rejoin_cooldown_minutes: Math.max(0, parseInt(el.inputCooldownMin.value || "0", 10)),
        max_active_slots: Math.max(0, parseInt(el.inputMaxSlots.value || "0", 10)),
        board_visible_queue: Math.max(1, Math.min(32, parseInt(el.inputVisibleCount.value || "7", 10))),
        queue_open: el.toggleQueueOpen.checked,
        auto_start_timer: el.toggleAutoStart.checked,
      });
      applyState(updated);
      el.settingsModal.classList.add("hidden");
      showToast("✅ Настройки сохранены");
    } catch (err) {
      showToast(err.message);
    }
  }

  async function openAddStudentModal() {
    if (!isControlsUnlocked()) {
      openPinModal();
      return;
    }
    try {
      rosterCache = await apiRequest("/api/students", "GET");
      const queuedCounts = {};
      (boardState?.queue || []).forEach((q) => {
        if (q.student_id) queuedCounts[q.student_id] = (queuedCounts[q.student_id] || 0) + 1;
      });
      if (boardState?.current?.student_id) {
        const cid = boardState.current.student_id;
        queuedCounts[cid] = (queuedCounts[cid] || 0) + 1;
      }

      el.selectRosterStudent.innerHTML = '<option value="">— Выберите из списка —</option>';
      rosterCache.forEach((st) => {
        const opt = document.createElement("option");
        opt.value = String(st.id);
        const cnt = queuedCounts[st.id] || 0;
        opt.textContent = cnt > 0 ? `${st.full_name} (в очереди ×${cnt})` : st.full_name;
        el.selectRosterStudent.appendChild(opt);
      });
      el.inputCustomStudent.value = "";
      el.inputInsertPos.value = "";
      el.addStudentModal.classList.remove("hidden");
    } catch (err) {
      showToast(err.message);
    }
  }

  async function submitAddStudent() {
    const selectedId = el.selectRosterStudent.value ? Number(el.selectRosterStudent.value) : null;
    const customName = el.inputCustomStudent.value.trim() || null;
    const posRaw = el.inputInsertPos.value.trim();
    const position = posRaw ? Math.max(1, parseInt(posRaw, 10)) : null;

    if (!selectedId && !customName) {
      showToast("Выберите ученика из списка или введите имя");
      return;
    }

    try {
      const res = await apiRequest("/api/queue/insert", "POST", {
        student_id: selectedId,
        custom_name: selectedId ? null : customName,
        position,
      });
      if (res.state) applyState(res.state);
      el.addStudentModal.classList.add("hidden");
      showToast("✅ Ученик добавлен в очередь");
    } catch (err) {
      showToast(err.message);
    }
  }

  function openPinModal() {
    el.pinError.classList.add("hidden");
    el.inputPin.value = "";
    el.pinModal.classList.remove("hidden");
    setTimeout(() => el.inputPin.focus(), 50);
  }

  async function verifyPin(pinToCheck, silent = false) {
    try {
      const resp = await fetch("/api/pin/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin: pinToCheck }),
      });
      const data = await resp.json();
      if (data.valid) {
        prefs.pin = pinToCheck;
        pinVerified = true;
        localStorage.setItem("sq_pin", pinToCheck);
        el.pinModal.classList.add("hidden");
        updateLockUI();
        if (boardState) renderQueueList(boardState);
        if (!silent) showToast("🔓 Управление доской разблокировано");
        return true;
      }
      pinVerified = false;
      if (!silent) el.pinError.classList.remove("hidden");
      return false;
    } catch {
      return false;
    }
  }

  // =========================================================================
  // Event Wiring & Keyboard Shortcuts
  // =========================================================================
  el.btnStartPause.addEventListener("click", toggleStartPause);
  el.btnNext.addEventListener("click", callNext);
  el.btnAddMinute.addEventListener("click", addOneMinute);
  el.btnResetTimer.addEventListener("click", resetTimer);
  el.btnSettings.addEventListener("click", openSettingsModal);
  el.btnSaveSettings.addEventListener("click", saveSettings);
  el.btnOpenAddModal.addEventListener("click", openAddStudentModal);
  el.btnConfirmAddStudent.addEventListener("click", submitAddStudent);
  el.btnDemoAdd.addEventListener("click", addDemoStudent);
  el.btnUnlockFromBanner.addEventListener("click", openPinModal);

  el.btnSoundToggle.addEventListener("click", () => {
    ensureAudio();
    prefs.soundEnabled = !prefs.soundEnabled;
    localStorage.setItem("sq_sound", prefs.soundEnabled ? "1" : "0");
    updateSoundUI();
    showToast(prefs.soundEnabled ? "🔔 Звук звонка включён" : "🔕 Звук выключен");
  });

  el.btnTestBell.addEventListener("click", () => {
    ensureAudio();
    playAlarmSound(prefs.alarmStyle, true);
    showToast("🔊 Проверка звонка");
  });

  el.btnModalTestBell.addEventListener("click", () => {
    ensureAudio();
    prefs.volume = Number(el.inputVolume.value);
    playAlarmSound(el.selectAlarmSound.value, true);
  });

  el.inputVolume.addEventListener("input", () => {
    el.volumeVal.textContent = el.inputVolume.value;
  });

  el.timerPresetChips.addEventListener("click", (ev) => {
    const chip = ev.target.closest(".chip");
    if (!chip) return;
    el.inputTimerMin.value = chip.dataset.min;
    highlightPresetChip(chip.dataset.min);
  });

  el.inputTimerMin.addEventListener("input", () => {
    highlightPresetChip(el.inputTimerMin.value);
  });

  el.btnUndoCall.addEventListener("click", async () => {
    try {
      const res = await apiRequest("/api/queue/undo", "POST");
      if (res.state) applyState(res.state);
      el.settingsModal.classList.add("hidden");
      showToast("↩️ Предыдущий ученик возвращён");
    } catch (err) {
      showToast(err.message);
    }
  });

  el.btnExportCsv.addEventListener("click", async () => {
    try {
      const headers = {};
      if (prefs.pin) headers["X-Board-Pin"] = prefs.pin;
      const resp = await fetch("/api/export.csv", { headers });
      if (resp.status === 403) {
        pinVerified = false;
        updateLockUI();
        openPinModal();
        return;
      }
      if (!resp.ok) throw new Error("Не удалось скачать отчёт");
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "seminar_report.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast("📥 Отчёт seminar_report.csv скачан");
    } catch (err) {
      showToast(err.message);
    }
  });

  el.btnClearQueue.addEventListener("click", async () => {
    if (!confirm("Очистить всю текущую очередь и сбросить кулдауны учеников?")) return;
    try {
      const res = await apiRequest("/api/queue/clear", "POST", { reset_cooldowns: true });
      if (res.state) applyState(res.state);
      el.settingsModal.classList.add("hidden");
      showToast("🧹 Очередь очищена");
    } catch (err) {
      showToast(err.message);
    }
  });

  el.btnToggleEdit.addEventListener("click", () => {
    if (!isControlsUnlocked()) {
      openPinModal();
      return;
    }
    editMode = !editMode;
    el.btnToggleEdit.classList.toggle("active", editMode);
    el.boardEditBar.classList.toggle("hidden", !editMode);
    if (boardState) renderQueueList(boardState);
  });

  el.btnShowMore.addEventListener("click", () => {
    showAllQueue = !showAllQueue;
    if (boardState) renderQueueList(boardState);
  });

  el.btnFullscreen.addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen?.().catch(() => {});
    } else {
      document.exitFullscreen?.().catch(() => {});
    }
  });

  el.btnPinLock.addEventListener("click", () => {
    if (pinVerified) {
      pinVerified = false;
      prefs.pin = "";
      localStorage.removeItem("sq_pin");
      updateLockUI();
      if (boardState) renderQueueList(boardState);
      showToast("🔒 Управление заблокировано");
    } else {
      openPinModal();
    }
  });

  el.btnSubmitPin.addEventListener("click", () => verifyPin(el.inputPin.value.trim(), false));
  el.inputPin.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") verifyPin(el.inputPin.value.trim(), false);
  });

  document.querySelectorAll("[data-close-modal]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById(btn.dataset.closeModal)?.classList.add("hidden");
    });
  });

  document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (ev) => {
      if (ev.target === backdrop) backdrop.classList.add("hidden");
    });
  });

  // Keyboard shortcuts for classroom clicker / laptop
  document.addEventListener("keydown", (ev) => {
    const tag = (ev.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag)) return;
    if (!el.settingsModal.classList.contains("hidden")) return;
    if (!el.addStudentModal.classList.contains("hidden")) return;
    if (!el.pinModal.classList.contains("hidden")) return;

    if (ev.code === "Space") {
      ev.preventDefault();
      if (isControlsUnlocked() && boardState?.current) toggleStartPause();
    } else if (ev.key === "Enter" || ev.key === "ArrowRight") {
      ev.preventDefault();
      if (isControlsUnlocked() && !el.btnNext.disabled) callNext();
    } else if (ev.key === "r" || ev.key === "R" || ev.key === "к" || ev.key === "К") {
      if (isControlsUnlocked() && boardState?.current) resetTimer();
    } else if (ev.key === "+" || ev.key === "=") {
      if (isControlsUnlocked() && boardState?.current) addOneMinute();
    } else if (ev.key === "f" || ev.key === "F" || ev.key === "а" || ev.key === "А") {
      el.btnFullscreen.click();
    } else if (ev.key === "m" || ev.key === "M" || ev.key === "ь" || ev.key === "Ь") {
      el.btnSoundToggle.click();
    }
  });

  // Wall clock updater
  function updateWallClock() {
    const now = new Date();
    el.wallClock.textContent = now.toLocaleTimeString("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  // =========================================================================
  // Bootstrap
  // =========================================================================
  async function init() {
    updateSoundUI();
    updateWallClock();
    setInterval(updateWallClock, 5000);
    setInterval(renderTimerTick, 100);

    // Support auto-unlocking whiteboard via URL parameter: ?pin=1234
    const urlParams = new URLSearchParams(window.location.search);
    const urlPin = urlParams.get("pin");
    if (urlPin) {
      await verifyPin(urlPin.trim(), true);
      // Clean ?pin=... from address bar so students looking at the projector don't see it
      const cleanUrl = window.location.pathname + window.location.hash;
      window.history.replaceState({}, "", cleanUrl);
    } else if (prefs.pin) {
      await verifyPin(prefs.pin, true);
    }

    // Initial HTTP snapshot + live WebSocket
    try {
      const resp = await fetch("/api/state");
      if (resp.ok) {
        const data = await resp.json();
        applyState(data);
      }
    } catch {
      // WebSocket will retry automatically
    }

    connectWebSocket();
  }

  init();
})();
