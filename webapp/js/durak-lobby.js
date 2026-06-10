/**
 * durak-lobby.js
 * ─────────────────────────────────────────────────────────────
 * Общая логика списка лобби + создания/входа для Дурака.
 * Подключается и в durak.html (список), и в durak-room.html (комната).
 *
 * Раньше эти функции дублировались в durak.html и ОТСУТСТВОВАЛИ в
 * durak-room.html, из-за чего ломались кнопки (ReferenceError) и
 * кнопка "Создать лобби" не работала. Теперь источник один.
 *
 * Навигация после создания/входа делегируется странице через
 * глобальный хук window.enterWaitingRoom(lobbyId, lobbyData):
 *   - durak.html      → редиректит на durak-room.html?lobby=ID
 *   - durak-room.html → показывает комнату ожидания на месте
 * Если хук не определён — фоллбэк-редирект на durak-room.html.
 *
 * Состояние инкапсулировано в IIFE (приватные let'ы не конфликтуют
 * с глобальными let currentUserId/currentLobbyId на страницах).
 * ─────────────────────────────────────────────────────────────
 */

(function () {
  'use strict';

  let lobbies = [];
  let filter = 'all';
  let listTimer = null;

  // ── Запрещённые слова в названии лобби ───────────────────────
  const FORBIDDEN_LOBBY_WORDS = [
    'хуй', 'хуи', 'хуя', 'хер', 'пизд', 'ебал', 'ебан', 'бля', 'сук', 'мудак',
    'дроч', 'пидор', 'педик', 'fuck', 'shit', 'cunt', 'dick',
  ];

  function containsForbiddenWords(text) {
    if (!text) return false;
    const lower = text.toLowerCase();
    return FORBIDDEN_LOBBY_WORDS.some((w) => lower.includes(w));
  }
  window.containsForbiddenWords = containsForbiddenWords;

  // ── Утилиты ──────────────────────────────────────────────────
  function api(url, opts) {
    const fn = window.apiFetch || fetch;
    return fn(url, opts);
  }

  function getUser() {
    return window.Telegram?.WebApp?.initDataUnsafe?.user || null;
  }

  /** Недостаточно choin для ставки → предлагаем пополнить в магазине. */
  function offerTopUp(detail) {
    const short = detail && detail.short;
    let msg = 'Недостаточно choin для ставки.';
    if (short) msg += ` Не хватает ${short * 10} choin.`;
    msg += '\nПерейти в магазин для пополнения?';
    if (confirm(msg)) window.location.href = 'shop.html';
  }
  window.offerTopUp = offerTopUp;

  function typeLabel(t) {
    return t === 'podkidnoy' ? 'Подкидной' : 'Переводной';
  }

  /** Делегирует навигацию странице (или фоллбэк-редирект). */
  function goToLobby(lobbyId, lobbyData) {
    if (typeof window.enterWaitingRoom === 'function') {
      window.enterWaitingRoom(lobbyId, lobbyData || null);
    } else {
      window.location.href = `durak-room.html?lobby=${lobbyId}`;
    }
  }

  // ════════════════════════════════════════════════════════════
  //  СПИСОК ЛОББИ
  // ════════════════════════════════════════════════════════════

  async function loadLobbies() {
    const container = document.getElementById('lobby-list');
    const empty = document.getElementById('empty-state');
    if (container) container.innerHTML = '';
    if (empty) empty.style.display = 'none';

    try {
      const res = await api('/api/durak/lobbies');
      const data = await res.json();
      lobbies = data.lobbies || [];
      window.DLobbyList = lobbies; // для отладки/совместимости
      renderFilteredLobbies();
    } catch (e) {
      console.error('[durak] Failed to load lobbies', e);
      if (empty) empty.style.display = 'block';
    }
  }
  window.loadLobbies = loadLobbies;

  function applyFilter(f, btnElement) {
    filter = f;
    document.querySelectorAll('.filter-btn').forEach((b) => b.classList.remove('active'));
    const target = btnElement || (typeof event !== 'undefined' ? event.target : null);
    if (target) target.classList.add('active');
    renderFilteredLobbies();
  }
  window.applyFilter = applyFilter;

  function renderFilteredLobbies() {
    const container = document.getElementById('lobby-list');
    const empty = document.getElementById('empty-state');
    if (!container) return;
    container.innerHTML = '';

    let list = lobbies;
    if (filter === 'small') list = list.filter((l) => l.max_players <= 3);
    else if (filter === 'large') list = list.filter((l) => l.max_players >= 4);
    else if (filter === 'bet') list = list.filter((l) => l.bet_amount > 0);

    if (list.length === 0) {
      if (empty) empty.style.display = 'block';
      return;
    }
    if (empty) empty.style.display = 'none';

    list.forEach((lobby) => {
      const card = document.createElement('div');
      card.className = 'lobby-card';
      if (lobby.bet_amount > 0) card.classList.add('lobby-card--with-bet');

      const betText = lobby.bet_amount > 0 ? ` • ${lobby.bet_amount * 10} choin` : '';
      const potText = (lobby.pot && lobby.pot > 0) ? ` (банк ${lobby.pot * 10} choin)` : '';
      const name = lobby.name || lobby.creator_name || 'Игрок';

      const info = document.createElement('div');
      info.innerHTML = `
        <div><strong>${escapeHtml(name)}</strong></div>
        <div class="lobby-meta">
          ${lobby.current_players}/${lobby.max_players} игроков •
          ${lobby.deck_size} карт •
          ${typeLabel(lobby.game_type)}${betText}${potText}
        </div>`;
      info.style.cursor = 'pointer';
      info.onclick = () => showLobbyPreview(lobby);

      const joinBtn = document.createElement('button');
      joinBtn.className = 'join-btn';
      joinBtn.textContent = 'Войти';
      joinBtn.onclick = (e) => { e.stopPropagation(); joinLobby(lobby.id, lobby); };

      card.appendChild(info);
      card.appendChild(joinBtn);
      container.appendChild(card);
    });
  }
  window.renderFilteredLobbies = renderFilteredLobbies;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function showLobbyPreview(lobby) {
    const betText = lobby.bet_amount > 0 ? `${lobby.bet_amount * 10} choin` : 'Без ставки';
    const potText = (lobby.pot && lobby.pot > 0) ? `\nБанк лобби: ${lobby.pot * 10} choin` : '';
    const message =
      `Лобби #${lobby.id}\n` +
      `Создатель: ${lobby.creator_name || 'Игрок'}\n\n` +
      `Игроки: ${lobby.current_players}/${lobby.max_players}\n` +
      `Колода: ${lobby.deck_size} карт\n` +
      `Тип: ${typeLabel(lobby.game_type)}\n` +
      `Шулерство: ${lobby.cheating_enabled ? 'Разрешено' : 'Запрещено'}\n` +
      `Ставка: ${betText}${potText}\n\n` +
      (lobby.bet_amount > 0
        ? 'Ставка списывается при входе и возвращается, если выйдешь до старта игры.\n\n'
        : '') +
      'Зайти в это лобби?';
    if (confirm(message)) joinLobby(lobby.id, lobby);
  }
  window.showLobbyPreview = showLobbyPreview;

  // ════════════════════════════════════════════════════════════
  //  СОЗДАНИЕ ЛОББИ
  // ════════════════════════════════════════════════════════════

  function openCreateLobbyModal() {
    const modal = document.getElementById('create-modal');
    if (!modal) {
      alert('Окно создания лобби не найдено');
      return;
    }
    // Прячем прочие модалки/оверлеи, которые могли «зависнуть» поверх и
    // перехватывать клики (после игры / bfcache-восстановления страницы).
    ['confirm-modal', 'edit-settings-modal'].forEach((id) => {
      const m = document.getElementById(id);
      if (m) m.style.display = 'none';
    });
    document.querySelectorAll('#no-energy-overlay, .dg-gameover').forEach((el) => {
      el.style.display = 'none';
    });
    modal.style.display = 'flex';
    const nameInput = document.getElementById('lobby-name');
    if (nameInput) nameInput.value = '';
  }
  window.openCreateLobbyModal = openCreateLobbyModal;

  function closeCreateModal() {
    const modal = document.getElementById('create-modal');
    if (modal) modal.style.display = 'none';
    const nameInput = document.getElementById('lobby-name');
    if (nameInput) nameInput.value = '';
  }
  window.closeCreateModal = closeCreateModal;

  async function createLobby() {
    const submitBtn = document.querySelector('#create-modal button[onclick="createLobby()"]');
    const original = submitBtn ? submitBtn.textContent : 'Создать';

    const user = getUser();
    if (!user) {
      alert('Не удалось получить данные пользователя Telegram');
      return;
    }

    let lobbyName = (document.getElementById('lobby-name')?.value || '').trim();
    if (!lobbyName) {
      const fn = user.first_name || 'Игрок';
      const templates = [
        `Лобби ${fn}`,
        `${fn} зовёт на Дурака`,
        `Дурак с ${fn}`,
        `Побоище ${fn}`,
      ];
      lobbyName = templates[Math.floor(Math.random() * templates.length)];
    }

    if (containsForbiddenWords(lobbyName)) {
      alert('Название лобби содержит недопустимые слова. Измени название.');
      return;
    }

    const settings = {
      user_id: user.id,
      first_name: user.first_name || '',
      photo_url: user.photo_url || null,
      name: lobbyName,
      max_players: parseInt(document.getElementById('max-players').value, 10),
      deck_size: parseInt(document.getElementById('deck-size').value, 10),
      game_type: document.getElementById('game-type')?.value || 'podkidnoy',
      cheating_enabled: document.getElementById('cheating')?.value === 'true',
      bet_amount: Math.floor((parseInt(document.getElementById('bet-amount').value, 10) || 0) / 10),
    };

    if (submitBtn) { submitBtn.textContent = 'Создаём…'; submitBtn.disabled = true; }

    try {
      const res = await api('/api/durak/lobbies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      });
      const data = await res.json();
      if (res.status === 402) { closeCreateModal(); offerTopUp(data.detail); return; }
      if (!res.ok) throw new Error((data.detail && data.detail.reason) ? 'Ошибка ставки' : (data.detail || 'Ошибка создания лобби'));
      if (!data.lobby_id) throw new Error('Сервер не вернул ID лобби');

      closeCreateModal();
      goToLobby(data.lobby_id, null);
    } catch (e) {
      console.error('[durak] Create lobby error', e);
      alert('Не удалось создать лобби: ' + (e.message || e));
    } finally {
      if (submitBtn) { submitBtn.textContent = original; submitBtn.disabled = false; }
    }
  }
  window.createLobby = createLobby;

  // ════════════════════════════════════════════════════════════
  //  ВХОД В ЛОББИ
  // ════════════════════════════════════════════════════════════

  async function joinLobby(lobbyId, lobbyData) {
    const user = getUser();
    if (!user) {
      alert('Не удалось получить данные пользователя');
      return;
    }

    // Если мы создатель — заходим без повторного join
    if (lobbyData && lobbyData.creator_id === user.id) {
      goToLobby(lobbyId, lobbyData);
      return;
    }

    try {
      const res = await api(`/api/durak/lobbies/${lobbyId}/join`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: user.id, first_name: user.first_name || '', photo_url: user.photo_url || null }),
      });
      if (res.status === 402) {
        const d = await res.json().catch(() => ({}));
        offerTopUp(d.detail);
        return;
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const detail = (err.detail && typeof err.detail === 'string') ? err.detail : 'Не удалось войти в лобби';
        throw new Error(detail);
      }
      goToLobby(lobbyId, lobbyData);
    } catch (e) {
      const m = (e.message || '').toLowerCase();
      if (m.includes('already in') || m.includes('уже в')) {
        goToLobby(lobbyId, lobbyData);
        return;
      }
      // Ожидаемые причины (лобби закрыто/удалено/заполнено/не существует) —
      // это нормально (сам вышел и т.п.): без резкого алерта, просто обновляем список.
      const expected = ['not available', 'not found', 'full', 'недоступ', 'не найдено', 'заполнено', '404'];
      const isExpected = expected.some((w) => m.includes(w));
      if (!isExpected) alert('Ошибка входа: ' + e.message);
      if (typeof loadLobbies === 'function' && document.getElementById('lobby-list')) {
        loadLobbies();
        if (typeof startLobbyListPolling === 'function') startLobbyListPolling();
      }
    }
  }
  window.joinLobby = joinLobby;

  // ════════════════════════════════════════════════════════════
  //  ПОЛЛИНГ СПИСКА
  // ════════════════════════════════════════════════════════════

  function startLobbyListPolling() {
    stopLobbyListPolling();
    listTimer = setInterval(() => {
      const listView = document.getElementById('lobby-list-view');
      const inRoom = typeof window.isInWaitingRoom === 'function'
        ? window.isInWaitingRoom()
        : false;
      if (listView && listView.style.display !== 'none' && !inRoom) {
        loadLobbies();
      }
    }, 8000);
  }
  window.startLobbyListPolling = startLobbyListPolling;

  function stopLobbyListPolling() {
    if (listTimer) { clearInterval(listTimer); listTimer = null; }
  }
  window.stopLobbyListPolling = stopLobbyListPolling;
})();
