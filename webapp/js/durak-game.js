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
    prevDeck: null,
    pollMs: 1500,
  };

  // экспорт наружу для отладки/совместимости
  window.DG = DG;

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
    DG.state = state;
    window.currentGameState = state;

    renderTopBar(state);
    renderOpponents(state);
    renderDeckAndTrump(state);
    renderDiscard(state);
    renderTable(state);
    renderHand(state);
    renderActions(state);

    if (state.game_over) showGameOver(state);
  }
  window.renderDurakBoard = render;

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
      if (pid === state.attacker) tile.classList.add('is-attacker');
      if (pid === state.defender) tile.classList.add('is-defender');
      // Офлайн-индикатор: connected приходит в state (из /state и бродкастов)
      if (state.connected && !state.connected.includes(pid)) tile.classList.add('offline');

      const count = typeof state.hands[pid] === 'number'
        ? state.hands[pid]
        : (Array.isArray(state.hands[pid]) ? state.hands[pid].length : 0);

      // Карты «в руках» соперника — веер рубашек перед иконкой
      const fan = document.createElement('div');
      fan.className = 'dg-opp-fan';
      const fanCount = Math.min(5, Math.max(1, count));
      for (let k = 0; k < fanCount; k++) {
        const b = createBack();
        const o = k - (fanCount - 1) / 2;
        b.style.left = '50%';
        b.style.marginLeft = (o * 9 - 12) + 'px';
        b.style.transform = `rotate(${o * 11}deg)`;
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

  function renderTable(state) {
    const zone = document.getElementById('dg-table');
    if (!zone) return;
    zone.innerHTML = '';

    const table = state.table || [];
    const unbeatenAttacks = table.filter((p) => !p.beat).map((p) => p.attack);

    table.forEach((pair) => {
      const slot = document.createElement('div');
      slot.className = 'dg-slot';

      const atk = createCard(pair.attack);
      atk.classList.add('dg-attack');
      slot.appendChild(atk);

      if (pair.beat) {
        const bt = createCard(pair.beat);
        bt.classList.add('dg-beat');
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

  function renderHand(state) {
    const handEl = document.getElementById('dg-hand');
    if (!handEl) return;
    handEl.innerHTML = '';

    const my = state.hands ? state.hands[DG.userId] : null;
    if (!my || !Array.isArray(my)) return;

    const legalAttacks = new Set(state.legal_attacks || []);
    const beatable = new Set((state.legal_beats || []).map((b) => b.beat));
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
    sorted.forEach((cardStr, i) => {
      const card = createCard(cardStr);
      card.classList.add('dg-hand-card');

      // веер-дуга: крайние карты чуть ниже (translateY в экранных координатах)
      const mid = (total - 1) / 2;
      const offset = i - mid;
      const rot = offset * Math.min(5, 40 / Math.max(total, 1));
      const lift = offset * offset * 2.2;
      card.style.transform = `translateY(${lift}px) rotate(${rot}deg)`;
      card.style.zIndex = String(10 + i);

      const playable = isDefender ? beatable.has(cardStr) : legalAttacks.has(cardStr);
      if (playable) card.classList.add('playable');
      else card.classList.add('dimmed');

      if (DG.selectedHandCard === cardStr) card.classList.add('selected');

      card.onclick = () => onHandCardClick(cardStr);
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

  async function doAction(action, extra) {
    const s = DG.state;
    if (DG.busy || !s || s.game_over) return;
    DG.busy = true;
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
    const url = `${proto}//${host}/api/durak/ws/${DG.lobbyId}?user_id=${DG.userId}`;
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
          showFlyingReaction(msg.emojiName, msg.position || 'self');
        } else if (type === 'presence') {
          if (DG.state) { DG.state.connected = msg.connected || []; render(DG.state); }
        }
      };
      ws.onclose = () => { DG.socket = null; };
      ws.onerror = () => {};
    } catch (e) {
      console.warn('[durak] WS unavailable, polling only', e);
    }
  }

  // ════════════════════════════════════════════════════════════
  //  ИГРА ОКОНЧЕНА
  // ════════════════════════════════════════════════════════════

  function showGameOver(state) {
    stopPolling();
    const overlay = document.getElementById('dg-gameover');
    if (!overlay) return;
    const titleEl = document.getElementById('dg-go-title');
    const subEl = document.getElementById('dg-go-sub');

    const me = DG.userId;
    const isDurak = state.durak != null && state.durak === me;
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
    showFlyingReaction(name, 'self');
    if (DG.socket && DG.socket.readyState === WebSocket.OPEN) {
      DG.socket.send(JSON.stringify({
        action: 'reaction',
        lobby_id: DG.lobbyId,
        user_id: DG.userId,
        data: { emojiName: name, position: 'self' },
      }));
    }
  }

  function showFlyingReaction(name, position) {
    const host = document.getElementById('durak-board') || document.body;
    const el = document.createElement('div');
    el.className = 'dg-flying-reaction';
    el.innerHTML = `<img src="icons/emoji/${name}.png" draggable="false">`;
    const left = position === 'self' ? '50%' : (30 + Math.random() * 40) + '%';
    el.style.left = left;
    el.style.bottom = position === 'self' ? '140px' : '60%';
    host.appendChild(el);
    setTimeout(() => el.remove(), 1900);
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

    Promise.all([loadPlayersDirectory(), loadLobbyMeta()]).then(() => {
      loadState().then(() => {
        startPolling();
        initEmotions();
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
