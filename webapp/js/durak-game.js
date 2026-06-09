/**
 * durak-game.js
 * ─────────────────────────────────────────────────────────────
 * Игровой клиент Дурака для полноэкранного поля (durak-game.html).
 *
 * Отвечает за:
 *   - загрузку состояния игры (GET /api/durak/lobbies/{id}/state)
 *   - рендер поля (соперники, колода, козырь, стол, рука, бито)
 *   - взаимодействие с картами (выбор/клик, drag&drop)
 *   - совершение ходов (POST /api/durak/lobbies/{id}/action)
 *   - realtime через WebSocket (+ поллинг как фоллбэк)
 *   - подсветку ролей (атакующий/защитник/ваш ход)
 *
 * Контракт состояния (get_full_game_state на сервере):
 *   players, attacker, defender, trump_suit, game_type,
 *   hands {pid: cards[] | count}, table [{attack, beat}],
 *   discard_count, deck_remaining, phase, game_over, winner,
 *   role, allowed_actions, legal_attacks [str],
 *   legal_beats [{attack, beat}], players_who_can_throw_in,
 *   can_attack_more, can_finish_attack, can_take_table,
 *   max_attack_cards_remaining
 *
 * Контракт действия (POST /action):
 *   { user_id, action, card?, attack_card?, beat_card? }
 *   action ∈ attack | throw_in | beat | take_table | finish_attack
 * ─────────────────────────────────────────────────────────────
 */

(function () {
  'use strict';

  // ── Глобальное состояние клиента ───────────────────────────
  const DG = {
    lobbyId: null,
    userId: null,
    state: null,
    pollTimer: null,
    socket: null,
    busy: false,            // защита от двойных кликов
    selectedHandCard: null, // выбранная карта руки (для отбоя защитником)
    transferMode: false,    // режим выбора карты для перевода (perevodnoy)
    prevDeck: null,
    pollMs: 1500,
  };

  // экспорт наружу для отладки/совместимости
  window.DG = DG;

  // ── Звук (WebAudio-синтез, без ассетов) ───────────────────────
  const Sound = (function () {
    let ctx = null;
    let enabled = localStorage.getItem('durakSound') !== '0';
    function ac() {
      if (!ctx) {
        try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {}
      }
      return ctx;
    }
    function beep(freq, dur, type, vol) {
      if (!enabled) return;
      const c = ac();
      if (!c) return;
      try {
        const o = c.createOscillator(), g = c.createGain();
        o.type = type || 'sine';
        o.frequency.value = freq;
        const t = c.currentTime;
        g.gain.setValueAtTime(vol || 0.06, t);
        g.gain.exponentialRampToValueAtTime(0.0001, t + (dur || 0.12));
        o.connect(g); g.connect(c.destination);
        o.start(t); o.stop(t + (dur || 0.12));
      } catch (e) {}
    }
    return {
      play:  () => beep(330, 0.10, 'triangle', 0.06),
      beat:  () => beep(520, 0.09, 'square', 0.05),
      take:  () => beep(170, 0.24, 'sawtooth', 0.05),
      bito:  () => { beep(440, 0.07); setTimeout(() => beep(660, 0.11), 70); },
      turn:  () => beep(720, 0.11, 'sine', 0.07),
      win:   () => [523, 659, 784].forEach((f, i) => setTimeout(() => beep(f, 0.17, 'triangle', 0.07), i * 120)),
      lose:  () => [392, 330, 262].forEach((f, i) => setTimeout(() => beep(f, 0.20, 'sawtooth', 0.05), i * 140)),
      toggle: () => { enabled = !enabled; localStorage.setItem('durakSound', enabled ? '1' : '0'); return enabled; },
      isOn: () => enabled,
    };
  })();
  window.DurakSound = Sound;

  // ── Утилита запросов ───────────────────────────────────────
  function api(url, opts) {
    const fn = window.apiFetch || fetch;
    return fn(url, opts);
  }

  // ════════════════════════════════════════════════════════════
  //  КАРТЫ (реальные ассеты из icons/durak/)
  // ════════════════════════════════════════════════════════════

  const SUIT_MAP = {
    '♥': { name: 'hearts',   color: 'red'   },
    '♦': { name: 'diamonds', color: 'red'   },
    '♣': { name: 'clubs',    color: 'black' },
    '♠': { name: 'spades',   color: 'black' },
  };

  function splitCard(cardStr) {
    return { rank: cardStr.slice(0, -1), suit: cardStr.slice(-1) };
  }

  function displayRank(rank) {
    if (rank === 'J' || rank === '11') return 'В';
    if (rank === 'Q' || rank === '12') return 'Д';
    if (rank === 'K' || rank === '13') return 'К';
    if (rank === 'A' || rank === '14' || rank === '1') return 'Т';
    return rank;
  }

  /** Создаёт DOM-элемент реальной карты по строке вида "10♥". */
  function createCard(cardStr) {
    const el = document.createElement('div');
    el.className = 'rdcard';
    el.dataset.card = cardStr;

    const { rank, suit } = splitCard(cardStr);
    const suitInfo = SUIT_MAP[suit];
    if (!suitInfo) {
      el.innerHTML = `<div class="rdcard-fallback">${cardStr}</div>`;
      return el;
    }
    el.classList.add(suitInfo.color);

    const base = document.createElement('img');
    base.src = 'icons/durak/card_front.png';
    base.className = 'rdcard-base';
    base.draggable = false;
    el.appendChild(base);

    const dr = displayRank(rank);
    const aceCard = ['A', '14', '1'].includes(rank);
    const corner = (cls) => {
      const c = document.createElement('div');
      c.className = `rdcard-corner ${cls} ${suitInfo.color}`;
      // На тузах масть в углах не нужна — её показывает крупная картинка в центре
      c.innerHTML = `<span class="rdcard-rank">${dr}</span>` +
                    (aceCard ? '' : `<img src="icons/durak/${suitInfo.name}.png" class="rdcard-suit-sm" draggable="false">`);
      return c;
    };
    el.appendChild(corner('tl'));
    el.appendChild(corner('br'));

    const center = document.createElement('div');
    center.className = 'rdcard-center';
    const isCourt = ['J','Q','K','11','12','13'].includes(rank);
    const isAce = ['A','14','1'].includes(rank);
    if (isCourt) {
      let base = 'jack';
      if (rank === 'Q' || rank === '12') base = 'queen';
      if (rank === 'K' || rank === '13') base = 'king';
      // Для красных мастей (червы/бубны) — красные версии фигур
      const file = suitInfo.color === 'red' ? `${base}_red.png` : `${base}.png`;
      const img = document.createElement('img');
      img.src = `icons/durak/${file}`;
      img.className = 'rdcard-court';
      if (base === 'jack') img.className += ' rdcard-jack';
      img.draggable = false;
      center.appendChild(img);
    } else if (isAce) {
      const img = document.createElement('img');
      img.src = `icons/durak/${suitInfo.name}_ace.png`;
      img.className = 'rdcard-ace';
      img.draggable = false;
      center.appendChild(img);
    } else {
      // Числовые карты (2–10): крупная цифра номинала по центру
      const big = document.createElement('div');
      big.className = 'rdcard-bignum';
      big.textContent = rank;
      center.appendChild(big);
    }
    el.appendChild(center);
    return el;
  }

  // Экспортируем рендер карт наружу (для страницы превью durak-cards.html)
  window.createDurakCard = createCard;
  window.createDurakBack = createBack;

  /** Карта-рубашка (для соперников / колоды / бито). */
  function createBack() {
    const el = document.createElement('div');
    el.className = 'rdback';
    return el;
  }

  // ════════════════════════════════════════════════════════════
  //  РЕНДЕР
  // ════════════════════════════════════════════════════════════

  function isMyTurnHand() {
    const s = DG.state;
    if (!s) return false;
    return (s.legal_attacks && s.legal_attacks.length > 0) ||
           (s.legal_beats && s.legal_beats.length > 0);
  }

  function render(state) {
    if (!state) return;
    const prev = DG.state;
    DG.state = state;
    window.currentGameState = state;
    // Сбрасываем режим перевода если мы больше не защитник
    if (state.role !== 'defender') DG.transferMode = false;

    renderTopBar(state);
    updateTurnUI(state);
    renderOpponents(state);
    renderDeckAndTrump(state);
    renderDiscard(state);
    renderTable(state, prev);
    renderHand(state, prev);
    renderActions(state);

    reactToChanges(state, prev);
    animateTransitions(prev, state);

    if (state.game_over) showGameOver(state);
  }
  window.renderDurakBoard = render;

  // ── Индикатор хода + экранный таймер ──────────────────────────
  function updateTurnUI(state) {
    const box = document.getElementById('dg-turn');
    const txt = document.getElementById('dg-turn-text');
    if (!box) return;
    if (state.game_over || state.active_player == null) {
      box.classList.add('hidden');
      stopTurnTimer();
      return;
    }
    box.classList.remove('hidden');
    const mine = String(state.active_player) === String(DG.userId);
    box.classList.toggle('my-turn', mine);
    if (txt) txt.textContent = mine ? 'Ваш ход' : ('Ход: ' + shortName(state.active_player));

    const tt = state.turn_timeout_sec || 60;
    // Новый ход (сменился игрок или обновился last_action_at) → сброс дедлайна
    if (DG._lastActionAt !== state.last_action_at || DG._turnFor !== state.active_player) {
      DG._lastActionAt = state.last_action_at;
      DG._turnFor = state.active_player;
      DG._turnTotal = tt;
      DG._turnDeadline = Date.now() + tt * 1000;
    }
    startTurnTimer();
  }

  function startTurnTimer() {
    if (DG._turnInt) return;
    DG._turnInt = setInterval(tickTurn, 250);
    tickTurn();
  }
  function stopTurnTimer() {
    if (DG._turnInt) { clearInterval(DG._turnInt); DG._turnInt = null; }
  }
  function tickTurn() {
    const box = document.getElementById('dg-turn');
    const ring = document.getElementById('dg-turn-timer');
    const sec = document.getElementById('dg-turn-secs');
    if (!box || box.classList.contains('hidden') || !DG._turnDeadline) return;
    const remMs = Math.max(0, DG._turnDeadline - Date.now());
    const rem = Math.ceil(remMs / 1000);
    const frac = Math.max(0, Math.min(1, remMs / (DG._turnTotal * 1000)));
    if (sec) sec.textContent = rem;
    if (ring) ring.style.setProperty('--turn-deg', (frac * 360) + 'deg');
    box.classList.toggle('low', rem <= 10);
  }

  // ── Реакция на изменения состояния: звук + take-flash ─────────
  // ── Общая визуализация движения карт (колода→рука/соперники, стол→сброс/взятие) ──
  function _rect(el) { return el ? el.getBoundingClientRect() : null; }

  function flyGhost(from, to, cardStr, delay) {
    if (!from || !to) return;
    const g = cardStr ? createCard(cardStr) : createBack();
    g.style.cssText = `position:fixed;left:${from.left}px;top:${from.top}px;` +
      `width:${from.width}px;height:${from.height}px;margin:0;z-index:9998;pointer-events:none;` +
      `transform-origin:top left;opacity:0;` +
      `transition:transform .34s cubic-bezier(.25,.8,.3,1),opacity .15s;`;
    document.body.appendChild(g);
    const dx = to.left - from.left, dy = to.top - from.top;
    const sc = from.width ? (to.width / from.width) : 1;
    requestAnimationFrame(() => {
      g.style.opacity = '1';
      setTimeout(() => { g.style.transform = `translate(${dx}px,${dy}px) scale(${sc})`; }, delay || 0);
    });
    setTimeout(() => g.remove(), 360 + (delay || 0));
  }

  function _handCount(h, pid) {
    const v = h && h[pid];
    return typeof v === 'number' ? v : (Array.isArray(v) ? v.length : 0);
  }

  function animateTransitions(prev, state) {
    if (!prev) return;                       // первый рендер не анимируем
    const deckR = _rect(document.getElementById('dg-deck'));

    // 1) Мои новые карты (добор) ← колода
    const prevMy = (prev.hands && Array.isArray(prev.hands[DG.userId])) ? prev.hands[DG.userId] : [];
    const my = (state.hands && Array.isArray(state.hands[DG.userId])) ? state.hands[DG.userId] : [];
    if (deckR) {
      my.filter((c) => !prevMy.includes(c)).forEach((c, i) => {
        const hel = document.querySelector(`#dg-hand .dg-hand-card[data-card="${c}"]`);
        flyGhost(deckR, _rect(hel), c, i * 55);
      });
    }

    // 2) Соперники добрали ← колода (по росту количества карт)
    if (deckR) {
      (state.players || []).forEach((pid) => {
        if (String(pid) === String(DG.userId)) return;
        const delta = _handCount(state.hands, pid) - _handCount(prev.hands, pid);
        if (delta > 0) {
          const oppR = _rect(document.querySelector(`.dg-opp[data-uid="${pid}"]`));
          for (let i = 0; i < Math.min(delta, 4); i++) flyGhost(deckR, oppR, null, i * 55);
        }
      });
    }

    // 3) Стол очистился → бито (в сброс) или взятие (к защитнику)
    const pLen = (prev.table || []).length;
    if (pLen > 0 && (state.table || []).length === 0) {
      const tableR = _rect(document.getElementById('dg-table'));
      const took = (state.discard_count || 0) <= (prev.discard_count || 0);
      let toR;
      if (took) {
        const def = prev.defender;
        toR = String(def) === String(DG.userId)
          ? _rect(document.getElementById('dg-hand'))
          : _rect(document.querySelector(`.dg-opp[data-uid="${def}"]`));
      } else {
        toR = _rect(document.getElementById('dg-discard'));
      }
      if (tableR && toR) {
        for (let i = 0; i < Math.min(pLen, 5); i++) flyGhost(tableR, toR, null, i * 40);
      }
    }
  }

  function reactToChanges(state, prev) {
    // Звук «ваш ход»
    const meActive = String(state.active_player) === String(DG.userId);
    const wasActive = prev && String(prev.active_player) === String(DG.userId);
    if (!state.game_over && meActive && !wasActive) {
      Sound.turn();
      if (window.Telegram?.WebApp?.HapticFeedback) {
        try { window.Telegram.WebApp.HapticFeedback.impactOccurred('light'); } catch (e) {}
      }
    }
    if (!prev) return;

    const pTable = prev.table || [];
    const cTable = state.table || [];
    const prevAttacks = new Set(pTable.map((p) => p.attack));
    const prevBeats = new Set(pTable.filter((p) => p.beat).map((p) => p.attack));
    const newAttacks = cTable.filter((p) => !prevAttacks.has(p.attack)).length;
    const newBeats = cTable.filter((p) => p.beat && !prevBeats.has(p.attack)).length;

    if (pTable.length > 0 && cTable.length === 0) {
      // Кон закрылся: бито (карты ушли в сброс) или взятие
      if ((state.discard_count || 0) > (prev.discard_count || 0)) {
        Sound.bito();
      } else {
        Sound.take();
        const zone = document.getElementById('dg-table');
        if (zone) {
          zone.classList.remove('take-flash');
          void zone.offsetWidth;
          zone.classList.add('take-flash');
          setTimeout(() => zone.classList.remove('take-flash'), 520);
        }
      }
    } else {
      if (newAttacks > 0) Sound.play();
      if (newBeats > 0) Sound.beat();
    }
  }

  function renderTopBar(state) {
    const trumpMini = document.getElementById('dg-trump-badge');
    if (trumpMini && state.trump_suit) {
      const info = SUIT_MAP[state.trump_suit];
      if (info) {
        trumpMini.innerHTML = `<img src="icons/durak/${info.name}.png" draggable="false">`;
      }
    }
    const phaseEl = document.getElementById('dg-phase');
    if (phaseEl) {
      let txt = '';
      if (state.role === 'attacker') txt = 'Вы атакуете';
      else if (state.role === 'defender') txt = 'Вы защищаетесь';
      else txt = 'Ожидание';
      if (state.game_over) txt = 'Игра окончена';
      phaseEl.textContent = txt;
    }
  }

  function shortName(pid) {
    const players = (window.durakPlayersById) || {};
    const p = players[pid];
    if (p && p.first_name) return p.first_name;
    return '#' + String(pid).slice(-4);
  }

  function avatarLetter(pid) {
    const n = shortName(pid);
    return (n && n[0]) ? n[0].toUpperCase() : '?';
  }

  function renderOpponents(state) {
    const wrap = document.getElementById('dg-opponents');
    if (!wrap || !state.hands) return;
    wrap.innerHTML = '';

    const others = (state.players || []).filter(
      (p) => String(p) !== String(DG.userId)
    );

    others.forEach((pid) => {
      const tile = document.createElement('div');
      tile.className = 'dg-opp';
      tile.dataset.uid = pid;   // для привязки летящих реакций
      if (pid === state.attacker) tile.classList.add('is-attacker');
      if (pid === state.defender) tile.classList.add('is-defender');
      // Офлайн-индикатор: connected приходит в state (из /state и бродкастов)
      if (state.connected && !state.connected.includes(pid)) tile.classList.add('offline');

      const count = typeof state.hands[pid] === 'number'
        ? state.hands[pid]
        : (Array.isArray(state.hands[pid]) ? state.hands[pid].length : 0);

      // Веер рубашек = РЕАЛЬНОЕ число карт соперника (с уплотнением)
      const fan = document.createElement('div');
      fan.className = 'dg-opp-fan';
      const shown = Math.min(Math.max(count, 0), 18);   // кап для адекватной ширины
      const step = shown > 1 ? Math.min(7, 54 / (shown - 1)) : 0;
      const totalW = step * (shown - 1);
      for (let k = 0; k < shown; k++) {
        const b = createBack();
        b.style.left = `calc(50% + ${(k * step) - totalW / 2}px)`;
        b.style.marginLeft = '-11px';
        b.style.transform = `rotate(${(k - (shown - 1) / 2) * 4}deg)`;
        fan.appendChild(b);
      }

      const av = document.createElement('div');
      av.className = 'dg-opp-avatar';
      const photo = window.durakPlayersById && window.durakPlayersById[pid] && window.durakPlayersById[pid].photo_url;
      if (photo) {
        const img = document.createElement('img');
        img.src = photo;
        img.draggable = false;
        av.appendChild(img);
      } else {
        av.textContent = avatarLetter(pid);
      }

      const name = document.createElement('div');
      name.className = 'dg-opp-name';
      name.textContent = shortName(pid);

      tile.appendChild(fan);
      tile.appendChild(av);
      tile.appendChild(name);
      wrap.appendChild(tile);
    });
  }

  function renderDeckAndTrump(state) {
    const remaining = state.deck_remaining || 0;

    const countEl = document.getElementById('dg-deck-count');
    if (countEl) countEl.textContent = remaining;

    // Реальная козырная карта торчит из-под колоды (пока колода не пуста)
    const trumpEl = document.getElementById('dg-trump-card');
    if (trumpEl) {
      trumpEl.innerHTML = '';
      if (remaining > 0 && state.trump_card) {
        trumpEl.appendChild(createCard(state.trump_card));
        trumpEl.style.display = '';
      } else {
        trumpEl.style.display = 'none';
      }
    }

    // Стопка рубашек поверх козыря
    const deckPile = document.getElementById('dg-deck');
    if (deckPile) {
      deckPile.innerHTML = '';
      if (remaining > 0) {
        deckPile.style.visibility = 'visible';
        deckPile.appendChild(createBack());
      } else {
        deckPile.style.visibility = 'hidden';
      }
    }
  }

  function renderDiscard(state) {
    const pile = document.getElementById('dg-discard');
    if (!pile) return;
    pile.innerHTML = '';
    const count = state.discard_count || 0;
    if (count <= 0) { pile.classList.add('empty'); return; }
    pile.classList.remove('empty');
    const layers = Math.min(4, Math.max(1, Math.floor(count / 4) + 1));
    for (let k = 0; k < layers; k++) {
      const b = createBack();
      b.style.position = 'absolute';
      b.style.left = (k * 2) + 'px';
      b.style.top = (k * 1.5) + 'px';
      b.style.transform = `rotate(${(k % 2 ? 4 : -3)}deg)`;
      pile.appendChild(b);
    }
  }

  function renderTable(state, prev) {
    const zone = document.getElementById('dg-table');
    if (!zone) return;
    zone.innerHTML = '';

    const table = state.table || [];
    const unbeatenAttacks = table.filter((p) => !p.beat).map((p) => p.attack);
    // Что было на столе в прошлом рендере — чтобы анимировать только новое
    const pTable = (prev && prev.table) || [];
    const prevAttacks = new Set(pTable.map((p) => p.attack));
    const prevBeats = new Set(pTable.filter((p) => p.beat).map((p) => p.attack));

    table.forEach((pair) => {
      const slot = document.createElement('div');
      slot.className = 'dg-slot';

      const atk = createCard(pair.attack);
      atk.classList.add('dg-attack');
      atk.dataset.card = pair.attack;
      if (!prevAttacks.has(pair.attack)) atk.classList.add('dg-anim');
      slot.appendChild(atk);

      if (pair.beat) {
        const bt = createCard(pair.beat);
        bt.classList.add('dg-beat');
        bt.dataset.card = pair.beat;
        if (!prevBeats.has(pair.attack)) bt.classList.add('dg-anim');
        slot.appendChild(bt);
      } else {
        atk.classList.add('unbeaten');
        // если защитник выбрал карту руки — подсветить, куда можно положить
        if (DG.selectedHandCard && canBeat(DG.selectedHandCard, pair.attack)) {
          atk.classList.add('beatable');
        }
        atk.onclick = () => onAttackSlotClick(pair.attack);
      }
      zone.appendChild(slot);
    });

    // динамическое перекрытие, чтобы помещалось
    const n = table.length;
    if (n > 3) {
      const overlap = Math.min(28, (n - 3) * 8);
      zone.style.setProperty('--slot-overlap', `-${overlap}px`);
    } else {
      zone.style.setProperty('--slot-overlap', '0px');
    }
  }

  function renderHand(state, prev) {
    const handEl = document.getElementById('dg-hand');
    if (!handEl) return;
    handEl.innerHTML = '';

    const my = state.hands ? state.hands[DG.userId] : null;
    if (!my || !Array.isArray(my)) return;

    // Новые карты (добор/раздача) анимируем; при первом рендере анимируем все
    const prevMy = prev && prev.hands && Array.isArray(prev.hands[DG.userId]) ? prev.hands[DG.userId] : null;
    const isNew = (c) => !prevMy || !prevMy.includes(c);

    const legalAttacks = new Set(state.legal_attacks || []);
    const beatable = new Set((state.legal_beats || []).map((b) => b.beat));
    const legalTransfers = new Set(state.legal_transfers || []);
    const isDefender = state.role === 'defender';

    // Сортировка: от младшей к старшей, козыри правее всех
    const rankVal = (r) => ({ J: 11, Q: 12, K: 13, A: 14 }[r] || parseInt(r, 10));
    const trump = state.trump_suit;
    const isTrump = (c) => c.slice(-1) === trump;
    const sorted = [...my].sort((a, b) => {
      const ta = isTrump(a), tb = isTrump(b);
      if (ta !== tb) return ta ? 1 : -1;
      return rankVal(a.slice(0, -1)) - rankVal(b.slice(0, -1));
    });

    const total = sorted.length;

    // Динамическое уплотнение: чем больше карт, тем плотнее, но угол
    // (номинал + масть, ~22px слева) никогда не перекрывается.
    const CARD_W = 66;
    const MIN_VISIBLE = 24;                 // минимум видимой левой полосы карты
    const MAX_OVERLAP = CARD_W - MIN_VISIBLE;
    const containerW = handEl.clientWidth || (window.innerWidth - 20);
    let overlap = 0;
    if (total > 1) {
      const needed = total * CARD_W;
      if (needed > containerW) {
        overlap = Math.ceil((needed - containerW) / (total - 1));
      }
      overlap = Math.max(0, Math.min(overlap, MAX_OVERLAP));
    }

    sorted.forEach((cardStr, i) => {
      const card = createCard(cardStr);
      card.classList.add('dg-hand-card');
      card.dataset.card = cardStr;
      if (isNew(cardStr)) {
        card.classList.add('dg-anim');
        card.style.animationDelay = (i * 0.04) + 's';
      }
      card.style.marginLeft = i === 0 ? '0' : `-${overlap}px`;

      // веер-дуга: крайние карты чуть ниже (translateY в экранных координатах)
      const mid = (total - 1) / 2;
      const offset = i - mid;
      const rot = offset * Math.min(5, 40 / Math.max(total, 1));
      const lift = offset * offset * 2.2;
      card.style.transform = `translateY(${lift}px) rotate(${rot}deg)`;
      card.style.zIndex = String(10 + i);

      let playable;
      if (DG.transferMode && isDefender) {
        playable = legalTransfers.has(cardStr);
        if (playable) card.classList.add('transfer-card');
      } else {
        playable = isDefender ? beatable.has(cardStr) : legalAttacks.has(cardStr);
      }
      if (playable) card.classList.add('playable');
      else card.classList.add('dimmed');

      if (DG.selectedHandCard === cardStr) card.classList.add('selected');

      if (playable) {
        card.addEventListener('touchstart', (e) => {
          const t = e.changedTouches[0];
          dragStart(cardStr, card, t.clientX, t.clientY, t.identifier);
          e.preventDefault();
        }, { passive: false });
        card.addEventListener('mousedown', (e) => {
          if (e.button !== 0) return;
          dragStart(cardStr, card, e.clientX, e.clientY, null);
        });
      } else {
        card.addEventListener('click', () => onHandCardClick(cardStr));
      }
      handEl.appendChild(card);
    });
  }

  function renderActions(state) {
    const bar = document.getElementById('dg-actions');
    if (!bar) return;
    bar.innerHTML = '';
    const allowed = state.allowed_actions || [];

    if (allowed.includes('take_table')) {
      bar.appendChild(makeActionBtn('Беру', 'take', () => doAction('take_table')));
    }
    if (allowed.includes('finish_attack')) {
      bar.appendChild(makeActionBtn('Бито', 'bito', () => doAction('finish_attack')));
    }
    if (allowed.includes('transfer')) {
      const btn = makeActionBtn('Перевести ↪', 'transfer', () => {
        // Включаем режим выбора карты для перевода
        DG.transferMode = !DG.transferMode;
        btn.classList.toggle('dg-action-active', DG.transferMode);
        render(state);
      });
      if (DG.transferMode) btn.classList.add('dg-action-active');
      bar.appendChild(btn);
    }
    // подсказка-пас для подкидывающего, когда больше нечего делать
    if (state.role !== 'defender' &&
        state.attack_in_progress &&
        !state.can_finish_attack &&
        (state.legal_attacks || []).length === 0) {
      const hint = document.createElement('div');
      hint.className = 'dg-action-hint';
      hint.textContent = 'Ждём соперника…';
      bar.appendChild(hint);
    }
    // подсказка при режиме перевода
    if (DG.transferMode && (state.legal_transfers || []).length > 0) {
      const hint = document.createElement('div');
      hint.className = 'dg-action-hint';
      hint.textContent = '↪ Выбери карту для перевода';
      bar.appendChild(hint);
    }
  }

  function makeActionBtn(label, variant, onClick) {
    const b = document.createElement('button');
    b.className = `dg-action-btn dg-action-${variant}`;
    b.textContent = label;
    b.onclick = onClick;
    return b;
  }

  // ════════════════════════════════════════════════════════════
  //  ВЗАИМОДЕЙСТВИЕ
  // ════════════════════════════════════════════════════════════

  function canBeat(beatCard, attackCard) {
    // зеркало серверного _can_beat, для подсветки на клиенте
    const s = DG.state;
    if (!s || !s.trump_suit) return false;
    const a = splitCard(attackCard);
    const b = splitCard(beatCard);
    const rankVal = (r) => {
      const map = { J: 11, Q: 12, K: 13, A: 14 };
      return map[r] || parseInt(r, 10);
    };
    const trump = s.trump_suit;
    if (b.suit === trump) {
      if (a.suit === trump) return rankVal(b.rank) > rankVal(a.rank);
      return true;
    }
    if (a.suit === b.suit) return rankVal(b.rank) > rankVal(a.rank);
    return false;
  }

  function onHandCardClick(cardStr) {
    const s = DG.state;
    if (!s || DG.busy || s.game_over) return;

    if (s.role === 'defender') {
      // Режим перевода (только perevodnoy)
      if (DG.transferMode) {
        const legalTransfers = new Set(s.legal_transfers || []);
        if (legalTransfers.has(cardStr)) {
          DG.transferMode = false;
          doAction('transfer', { card: cardStr });
          return;
        }
        flashInvalid();
        return;
      }
      // выбираем карту для отбоя; если ровно одна цель — бьём сразу
      DG.selectedHandCard = (DG.selectedHandCard === cardStr) ? null : cardStr;
      const targets = (s.table || [])
        .filter((p) => !p.beat && canBeat(cardStr, p.attack))
        .map((p) => p.attack);
      if (DG.selectedHandCard && targets.length === 1) {
        const target = targets[0];
        DG.selectedHandCard = null;
        doAction('beat', { attack_card: target, beat_card: cardStr });
        return;
      }
      render(s); // перерисуем подсветку
      return;
    }

    // атакующий / подкидывающий
    const legal = new Set(s.legal_attacks || []);
    if (!legal.has(cardStr)) {
      flashInvalid();
      return;
    }
    const action = s.attack_in_progress && s.attacker !== DG.userId ? 'throw_in'
                 : (s.table && s.table.length > 0 ? 'throw_in' : 'attack');
    doAction(action, { card: cardStr });
  }

  function onAttackSlotClick(attackCard) {
    const s = DG.state;
    if (!s || s.role !== 'defender' || DG.busy) return;
    if (!DG.selectedHandCard) return;
    if (!canBeat(DG.selectedHandCard, attackCard)) {
      flashInvalid();
      return;
    }
    const beatCard = DG.selectedHandCard;
    DG.selectedHandCard = null;
    doAction('beat', { attack_card: attackCard, beat_card: beatCard });
  }

  function flashInvalid() {
    if (window.Telegram?.WebApp?.HapticFeedback) {
      try { window.Telegram.WebApp.HapticFeedback.notificationOccurred('error'); } catch (e) {}
    }
  }

  function hapticLight() {
    try { window.Telegram?.WebApp?.HapticFeedback?.impactOccurred('light'); } catch(e) {}
  }
  function hapticSuccess() {
    try { window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred('success'); } catch(e) {}
  }

  // ════════════════════════════════════════════════════════════
  //  DRAG & DROP — нативное перетаскивание карт
  // ════════════════════════════════════════════════════════════

  const DRAG = {
    active: false,
    cardStr: null,
    clone: null,
    srcEl: null,
    homeRect: null,
    touchId: null,    // Touch.identifier для фильтрации multi-touch
    startX: 0, startY: 0,
    offsetX: 0, offsetY: 0,
    moved: false,
  };

  function _dragCleanup() {
    document.body.style.touchAction = '';
    document.documentElement.style.touchAction = '';
    if (DRAG.clone) { DRAG.clone.remove(); DRAG.clone = null; }
    if (DRAG.srcEl) { DRAG.srcEl.style.opacity = ''; DRAG.srcEl = null; }
    clearDropHighlights();
    DRAG.active   = false;
    DRAG.cardStr  = null;
    DRAG.homeRect = null;
    DRAG.touchId  = null;
    DRAG.moved    = false;
  }

  function _returnClone() {
    const clone = DRAG.clone;
    const home  = DRAG.homeRect;
    const srcEl = DRAG.srcEl;
    // Сразу сбрасываем DRAG — новый drag может стартовать не дожидаясь анимации
    DRAG.clone = null; DRAG.srcEl = null;
    DRAG.active = false; DRAG.cardStr = null;
    DRAG.homeRect = null; DRAG.touchId = null; DRAG.moved = false;
    document.body.style.touchAction = '';
    document.documentElement.style.touchAction = '';
    clearDropHighlights();
    if (!clone) { if (srcEl) srcEl.style.opacity = ''; return; }
    clone.style.transition = 'left .2s ease,top .2s ease,transform .2s ease,opacity .2s ease';
    if (home) { clone.style.left = home.left + 'px'; clone.style.top = home.top + 'px'; }
    clone.style.transform = 'scale(1) rotate(0deg)';
    clone.style.opacity = '0.4';
    setTimeout(() => {
      clone.remove();
      if (srcEl) srcEl.style.opacity = '';
    }, 210);
  }

  // ── dragStart ─────────────────────────────────────────────
  function dragStart(cardStr, srcEl, cx, cy, touchId) {
    const s = DG.state;
    if (!s || DG.busy || s.game_over) return;
    if (DRAG.active) _dragCleanup();

    const isDefender     = s.role === 'defender';
    const legalAttacks   = new Set(s.legal_attacks || []);
    const beatable       = new Set((s.legal_beats || []).map(b => b.beat));
    const legalTransfers = new Set(s.legal_transfers || []);
    const ok = DG.transferMode
      ? legalTransfers.has(cardStr)
      : (isDefender ? beatable.has(cardStr) : legalAttacks.has(cardStr));
    if (!ok) return;

    const rect = srcEl.getBoundingClientRect();
    DRAG.active   = true;
    DRAG.cardStr  = cardStr;
    DRAG.srcEl    = srcEl;
    DRAG.touchId  = touchId != null ? touchId : null;
    DRAG.homeRect = { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
    DRAG.startX   = cx;
    DRAG.startY   = cy;
    DRAG.offsetX  = cx - rect.left;
    DRAG.offsetY  = cy - rect.top;
    DRAG.moved    = false;

    const clone = srcEl.cloneNode(true);
    clone.style.cssText =
      `position:fixed;left:${rect.left}px;top:${rect.top}px;` +
      `width:${rect.width}px;height:${rect.height}px;` +
      `z-index:99999;pointer-events:none;transition:none;` +
      `transform:scale(1.08) rotate(-3deg);` +
      `box-shadow:0 8px 24px rgba(0,0,0,.55);opacity:1;margin:0;`;
    document.body.appendChild(clone);
    DRAG.clone = clone;
    // Запрещаем scroll на всём документе пока тянем карту
    document.body.style.touchAction = 'none';
    document.documentElement.style.touchAction = 'none';
    srcEl.style.opacity = '0.25';
    hapticLight();
  }

  function _dragMove(cx, cy) {
    if (!DRAG.active || !DRAG.clone) return;
    DRAG.clone.style.left = (cx - DRAG.offsetX) + 'px';
    DRAG.clone.style.top  = (cy - DRAG.offsetY) + 'px';
    if (!DRAG.moved) {
      const dx = cx - DRAG.startX, dy = cy - DRAG.startY;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) DRAG.moved = true;
    }
    highlightDropTarget(cx, cy);
  }

  function _dragEnd(cx, cy) {
    if (!DRAG.active) return;
    clearDropHighlights();
    if (!DRAG.moved) {
      const card = DRAG.cardStr;
      _dragCleanup();
      onHandCardClick(card);
      return;
    }
    const cardStr = DRAG.cardStr;
    DRAG.clone.style.display = 'none';
    const target = document.elementFromPoint(cx, cy);
    DRAG.clone.style.display = '';
    const ok = resolveDropTarget(cardStr, target, cx, cy);
    if (ok) { _dragCleanup(); } else { flashInvalid(); _returnClone(); }
  }

  // ── Touch handlers ────────────────────────────────────────
  // Вешаются на #dg-hand И на document — защита от двойного вызова через _handled флаг
  function _onTouchMove(e) {
    if (!DRAG.active) return;
    // touches — все активные пальцы (для move); changedTouches — только изменившиеся
    const list = e.touches.length ? e.touches : e.changedTouches;
    const t = DRAG.touchId != null
      ? Array.from(list).find(x => x.identifier === DRAG.touchId)
      : list[0];
    if (!t) return;
    // preventDefault блокирует scroll браузера на весь жест
    if (e.cancelable) e.preventDefault();
    _dragMove(t.clientX, t.clientY);
  }

  function _onTouchEnd(e) {
    if (!DRAG.active) return;
    // touchend: палец ушёл — его нет в e.touches, только в changedTouches
    const t = DRAG.touchId != null
      ? Array.from(e.changedTouches).find(x => x.identifier === DRAG.touchId)
      : e.changedTouches[0];
    if (!t) return;
    if (e.cancelable) e.preventDefault();
    _dragEnd(t.clientX, t.clientY);
  }

  function _onTouchCancel(e) {
    if (!DRAG.active) return;
    _returnClone();
  }

  // ── Mouse handlers (десктоп) ──────────────────────────────
  function _onMouseMove(e) { if (DRAG.active && DRAG.touchId == null) _dragMove(e.clientX, e.clientY); }
  function _onMouseUp(e)   { if (DRAG.active && DRAG.touchId == null) _dragEnd(e.clientX, e.clientY); }

  // ── Определяем куда упала карта и исполняем действие ─────
  // Возвращает true если ход принят
  function resolveDropTarget(cardStr, targetEl, cx, cy) {
    const s = DG.state;
    if (!s || DG.busy) return false;

    const isDefender = s.role === 'defender';
    const tableZone  = document.getElementById('dg-table');

    // ── Перевод (defender, perevodnoy) ────────────────────
    if (DG.transferMode && isDefender) {
      const legalTransfers = new Set(s.legal_transfers || []);
      if (!legalTransfers.has(cardStr)) return false;
      if (!_pointInEl(cx, cy, tableZone)) return false;
      DG.transferMode = false;
      hapticSuccess();
      doAction('transfer', { card: cardStr });
      return true;
    }

    // ── Отбой (defender) ──────────────────────────────────
    if (isDefender) {
      // Ищем конкретную атакующую карту под точкой
      let attackCard = null;
      document.querySelectorAll('#dg-table .dg-attack.unbeaten').forEach(el => {
        if (_pointInEl(cx, cy, el, 16) && canBeat(cardStr, el.dataset.card)) {
          attackCard = el.dataset.card;
        }
      });
      if (!attackCard) {
        // Бросили в зону стола — если одна незакрытая
        if (tableZone && _pointInEl(cx, cy, tableZone)) {
          const targets = (s.table || []).filter(p => !p.beat && canBeat(cardStr, p.attack));
          if (targets.length === 1) attackCard = targets[0].attack;
        }
      }
      if (!attackCard) return false;
      hapticSuccess();
      doAction('beat', { attack_card: attackCard, beat_card: cardStr });
      return true;
    }

    // ── Атака / подкидывание ──────────────────────────────
    const legal = new Set(s.legal_attacks || []);
    if (!legal.has(cardStr)) return false;
    if (!tableZone || !_pointInEl(cx, cy, tableZone)) return false;
    hapticSuccess();
    const action = s.attack_in_progress ? 'throw_in' : 'attack';
    doAction(action, { card: cardStr });
    return true;
  }

  function _pointInEl(x, y, el, pad) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const p = pad || 0;
    return x >= r.left - p && x <= r.right + p && y >= r.top - p && y <= r.bottom + p;
  }

  function highlightDropTarget(x, y) {
    clearDropHighlights();
    const s = DG.state;
    if (!s) return;
    const isDefender = s.role === 'defender';

    if (isDefender && !DG.transferMode) {
      document.querySelectorAll('#dg-table .dg-attack.unbeaten').forEach(el => {
        if (_pointInEl(x, y, el, 16) && canBeat(DRAG.cardStr, el.dataset.card)) {
          el.classList.add('drop-target');
        }
      });
    }
    // Всегда подсвечиваем зону стола если над ней
    const tableZone = document.getElementById('dg-table');
    if (tableZone && _pointInEl(x, y, tableZone)) {
      tableZone.classList.add('drop-target-zone');
    }
  }

  function clearDropHighlights() {
    document.querySelectorAll('.drop-target').forEach(el => el.classList.remove('drop-target'));
    document.getElementById('dg-table')?.classList.remove('drop-target-zone');
  }

  function initDragListeners() {
    // capture:true — срабатывает ДО любых других listeners (включая Telegram WebApp scroll)
    // passive:false — позволяет preventDefault() на touchmove
    const opts = { capture: true, passive: false };
    document.addEventListener('touchmove',   _onTouchMove,   opts);
    document.addEventListener('touchend',    _onTouchEnd,    { capture: true, passive: false });
    document.addEventListener('touchcancel', _onTouchCancel, { capture: true, passive: true });
    document.addEventListener('mousemove',   _onMouseMove,   { capture: true });
    document.addEventListener('mouseup',     _onMouseUp,     { capture: true });
  }

  // Анимация полёта карты от руки к её месту на столе (FLIP клона)
  function flyCardToTable(cardStr, fromRect) {
    if (!fromRect) return;
    const tgt = document.querySelector(`#dg-table .dg-beat[data-card="${cardStr}"]`)
             || document.querySelector(`#dg-table .dg-attack[data-card="${cardStr}"]`);
    if (!tgt) return;
    const to = tgt.getBoundingClientRect();
    const clone = createCard(cardStr);
    clone.style.cssText = `position:fixed;left:${fromRect.left}px;top:${fromRect.top}px;` +
      `width:${fromRect.width}px;height:${fromRect.height}px;margin:0;z-index:99999;pointer-events:none;` +
      `transform-origin:top left;transition:transform .26s cubic-bezier(.25,.8,.3,1),opacity .26s;`;
    document.body.appendChild(clone);
    // Прячем реальную карту, пока летит клон. visibility (не opacity!) — иначе
    // её перебивает анимация появления dg-anim и видно «двойника».
    tgt.classList.remove('dg-anim');
    tgt.style.animation = 'none';
    tgt.style.visibility = 'hidden';
    const dx = to.left - fromRect.left, dy = to.top - fromRect.top;
    const sc = fromRect.width ? (to.width / fromRect.width) : 1;
    requestAnimationFrame(() => {
      clone.style.transform = `translate(${dx}px,${dy}px) scale(${sc})`;
    });
    setTimeout(() => { clone.remove(); if (tgt) tgt.style.visibility = ''; }, 280);
  }

  async function doAction(action, extra) {
    const s = DG.state;
    if (DG.busy || !s || s.game_over) return;
    DG.busy = true;
    // Запоминаем позицию сыгранной карты в руке ДО перерисовки
    const playedCard = extra && (extra.card || extra.beat_card);
    let srcRect = null;
    if (playedCard) {
      const hel = document.querySelector(`#dg-hand .dg-hand-card[data-card="${playedCard}"]`);
      if (hel) srcRect = hel.getBoundingClientRect();
    }
    try {
      const body = Object.assign({ user_id: DG.userId, action }, extra || {});
      const res = await api(`/api/durak/lobbies/${DG.lobbyId}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        console.warn('[durak] action rejected:', data.detail || res.status);
        flashInvalid();
      } else if (data.state) {
        DG.selectedHandCard = null;
        render(data.state);
        if (playedCard) flyCardToTable(playedCard, srcRect);
      }
    } catch (e) {
      console.error('[durak] action error', e);
    } finally {
      DG.busy = false;
    }
  }
  window.performDurakAction = (action, extra) => doAction(action, extra);

  // ════════════════════════════════════════════════════════════
  //  ЗАГРУЗКА / ПОЛЛИНГ / WS
  // ════════════════════════════════════════════════════════════

  async function loadState() {
    try {
      const res = await api(
        `/api/durak/lobbies/${DG.lobbyId}/state?user_id=${DG.userId || 0}`
      );
      if (!res.ok) return;
      const data = await res.json();
      if (data.state) render(data.state);
    } catch (e) {
      console.error('[durak] loadState error', e);
    }
  }
  window.fetchAndRenderDurakState = loadState;

  function startPolling() {
    stopPolling();
    DG.pollTimer = setInterval(() => {
      if (DG.state && DG.state.game_over) { stopPolling(); return; }
      if (!DG.busy) loadState();
    }, DG.pollMs);
  }
  window.startDurakGamePolling = startPolling;

  function stopPolling() {
    if (DG.pollTimer) { clearInterval(DG.pollTimer); DG.pollTimer = null; }
  }
  window.stopDurakGamePolling = stopPolling;

  // Лёгкий WS-клиент (тот же протокол, что и в комнате)
  function connectSocket() {
    const proto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
    const host = location.host || 'localhost:8000';
    const tgInit = (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) || '';
    let url = `${proto}//${host}/api/durak/ws/${DG.lobbyId}?user_id=${DG.userId}`;
    if (tgInit) url += `&init_data=${encodeURIComponent(tgInit)}`;
    try {
      const ws = new WebSocket(url);
      DG.socket = ws;
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        const type = msg.type || msg.event;
        if (type === 'game_action' && msg.state) {
          if (!DG.busy) render(msg.state);
        } else if (type === 'game_ended') {
          if (msg.final_state) render(msg.final_state);
          else loadState();
        } else if (type === 'reaction' && msg.emojiName) {
          showFlyingReaction(msg.emojiName, msg.user_id);
        } else if (type === 'presence') {
          if (DG.state) { DG.state.connected = msg.connected || []; render(DG.state); }
        }
      };
      ws.onopen = () => { if (DG._reconnectTimer) { clearTimeout(DG._reconnectTimer); DG._reconnectTimer = null; } };
      ws.onclose = () => { DG.socket = null; scheduleReconnect(); };
      ws.onerror = () => {};
    } catch (e) {
      console.warn('[durak] WS unavailable, polling only', e);
      scheduleReconnect();
    }
  }

  // Авто-переподключение WS, пока партия идёт — чтобы все видели реакции/онлайн
  function scheduleReconnect() {
    if (DG._reconnectTimer) return;
    if (DG.state && DG.state.game_over) return;
    DG._reconnectTimer = setTimeout(() => {
      DG._reconnectTimer = null;
      if (!(DG.state && DG.state.game_over)) connectSocket();
    }, 2500);
  }

  // ════════════════════════════════════════════════════════════
  //  ИГРА ОКОНЧЕНА
  // ════════════════════════════════════════════════════════════

  function showGameOver(state) {
    stopPolling();
    stopTurnTimer();
    const overlay = document.getElementById('dg-gameover');
    if (!overlay) return;
    const titleEl = document.getElementById('dg-go-title');
    const subEl = document.getElementById('dg-go-sub');

    const me = DG.userId;
    const isDurak = state.durak != null && state.durak === me;
    if (!DG._goSoundPlayed) {
      DG._goSoundPlayed = true;
      if (isDurak) Sound.lose(); else Sound.win();
    }
    let title;
    if (isDurak) title = 'Вы — дурак';
    else if (state.winner === me) title = 'Вы победили!';
    else title = 'Вы не дурак!';
    if (titleEl) titleEl.textContent = title;

    const emojiEl = document.getElementById('dg-go-emoji');
    if (emojiEl) {
      emojiEl.src = isDurak ? 'icons/emoji/durak.png' : 'icons/emoji/durak_win.png';
      emojiEl.style.display = 'block';
    }

    const pot = (window.currentLobbyData && window.currentLobbyData.pot) || 0;
    if (subEl) {
      subEl.textContent = pot > 0
        ? `Банк: ${pot} ⭐`
        : 'Игра без ставки';
    }
    overlay.style.display = 'flex';
  }
  window.showGameOver = showGameOver;

  function returnToLobby() {
    // Возврат всегда в меню Дурака (комната при playing редиректит обратно в игру → петля)
    location.href = 'durak.html';
  }

  async function surrender() {
    if (!confirm('Сдаться и выйти из игры?')) return;
    stopPolling();
    try {
      await api(`/api/durak/lobbies/${DG.lobbyId}/forfeit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: DG.userId }),
      });
    } catch (e) { /* всё равно уходим в меню */ }
    location.href = 'durak.html';
  }
  window.surrenderDurak = surrender;
  window.returnToLobby = returnToLobby;

  // ════════════════════════════════════════════════════════════
  //  ЭМОЦИИ (реакции)
  // ════════════════════════════════════════════════════════════

  const EMOJIS = ['like','lmao','cool','wow','angry','troll','kiss','evil','what','chill'];

  function initSoundToggle() {
    const sb = document.getElementById('dg-sound-btn');
    if (!sb) return;
    sb.textContent = Sound.isOn() ? '🔊' : '🔇';
    sb.onclick = () => {
      const on = Sound.toggle();
      sb.textContent = on ? '🔊' : '🔇';
      if (on) Sound.turn();
    };
  }

  function initEmotions() {
    const picker = document.getElementById('dg-emoji-picker');
    const btn = document.getElementById('dg-emoji-btn');
    if (!picker || !btn) return;
    picker.innerHTML = '';
    EMOJIS.forEach((name) => {
      const item = document.createElement('div');
      item.className = 'dg-emoji-item';
      item.innerHTML = `<img src="icons/emoji/${name}.png" draggable="false">`;
      item.onclick = () => {
        sendReaction(name);
        picker.classList.remove('show');
      };
      picker.appendChild(item);
    });
    btn.onclick = () => picker.classList.toggle('show');
  }
  window.initDurakEmotions = initEmotions;

  function sendReaction(name) {
    showFlyingReaction(name, DG.userId);
    if (DG.socket && DG.socket.readyState === WebSocket.OPEN) {
      DG.socket.send(JSON.stringify({
        action: 'reaction',
        lobby_id: DG.lobbyId,
        user_id: DG.userId,
        data: { emojiName: name },
      }));
    }
  }

  /** Эмодзи вылетает от отправителя (своя кнопка / плитка соперника) и летит
   *  к ЦЕНТРУ экрана — одинаково для игрока и для врага. */
  function showFlyingReaction(name, fromUserId) {
    let anchor = null;
    if (fromUserId && String(fromUserId) !== String(DG.userId)) {
      anchor = document.querySelector(`.dg-opp[data-uid="${fromUserId}"]`);
    }
    if (!anchor) anchor = document.getElementById('dg-emoji-btn');

    const r = anchor ? anchor.getBoundingClientRect() : null;
    const sx = r ? r.left + r.width / 2 : window.innerWidth / 2;
    const sy = r ? r.top + r.height / 2 : window.innerHeight - 120;
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;

    const el = document.createElement('div');
    el.className = 'dg-flying-reaction';
    el.innerHTML = `<img src="icons/emoji/${name}.png" draggable="false">`;
    el.style.left = sx + 'px';
    el.style.top = sy + 'px';
    document.body.appendChild(el);

    // Старт от отправителя → летим в центр (рост), держим, затем уплываем вверх и гаснем
    requestAnimationFrame(() => {
      el.style.left = cx + 'px';
      el.style.top = cy + 'px';
      el.classList.add('arrived');
    });
    setTimeout(() => el.classList.add('leaving'), 1000);
    setTimeout(() => el.remove(), 1600);
  }
  window.showFlyingReaction = showFlyingReaction;

  // ════════════════════════════════════════════════════════════
  //  ИНИЦИАЛИЗАЦИЯ
  // ════════════════════════════════════════════════════════════

  async function loadPlayersDirectory() {
    try {
      const res = await api(`/api/durak/lobbies/${DG.lobbyId}/players`);
      if (!res.ok) return;
      const data = await res.json();
      const map = {};
      (data.players || []).forEach((p) => { map[p.user_id] = p; });
      window.durakPlayersById = map;
    } catch (e) {}
  }

  async function loadLobbyMeta() {
    try {
      const res = await api(`/api/durak/lobbies/${DG.lobbyId}`);
      if (res.ok) window.currentLobbyData = await res.json();
    } catch (e) {}
  }

  function init() {
    const tg = window.Telegram?.WebApp;
    if (tg) { tg.ready(); tg.expand(); }

    const params = new URLSearchParams(location.search);
    const lobbyId = params.get('lobby') || params.get('lobby_id');
    DG.lobbyId = lobbyId ? parseInt(lobbyId, 10) : null;

    const user = tg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null;
    DG.userId = user ? user.id : (window.currentUserId || 0);
    window.currentUserId = DG.userId;
    window.currentLobbyId = DG.lobbyId;

    if (!DG.lobbyId) {
      // Не игровая страница (например профиль) — клиент игры не запускаем,
      // но window.createDurakCard уже доступен для превью карт.
      return;
    }

    // Кнопка назад → в комнату
    if (tg && tg.BackButton) {
      tg.BackButton.show();
      tg.BackButton.onClick(returnToLobby);
    }

    initDragListeners();

    Promise.all([loadPlayersDirectory(), loadLobbyMeta()]).then(() => {
      loadState().then(() => {
        startPolling();
        initEmotions();
        initSoundToggle();
        connectSocket();
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
