/**
 * Chin Games — Centralized API fetch helper
 * 
 * Automatically injects Telegram Init Data header for protected endpoints.
 * 
 * Usage:
 *   const res = await apiFetch('/api/save_score', {
 *     method: 'POST',
 *     body: JSON.stringify(payload)
 *   });
 *
 * All internal /api/* calls should use apiFetch instead of fetch.
 */

(function () {
  const ORIGINAL_FETCH = window.fetch;

  // Endpoints that should receive the X-Telegram-Init-Data header
  const PROTECTED_PREFIXES = [
    '/api/',
    'https://chin-games-bot.onrender.com/api/',
    // Add any other backend origins here if needed
  ];

  function isProtectedUrl(url) {
    const u = String(url || '');
    return PROTECTED_PREFIXES.some(prefix => u.includes(prefix));
  }

  /**
   * apiFetch(url, options)
   * Drop-in replacement for fetch that adds auth header for our API.
   */
  window.apiFetch = function apiFetch(url, options = {}) {
    if (isProtectedUrl(url)) {
      const tg = window.Telegram?.WebApp;
      const initData = (tg && tg.initData) ? tg.initData : '';

      const headers = {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': initData,
        ...(options.headers || {})
      };

      options = { ...options, headers };
    }

    return ORIGINAL_FETCH(url, options);
  };

  // Optional: expose the original fetch if someone really needs it
  window._originalFetch = ORIGINAL_FETCH;

  // Convenience: allow calling apiFetch with relative paths from anywhere
  window.API_BASE = ''; // can be overridden if needed

  // ─────────────────────────────────────────────────────────────
  // BAN / BLOCKED USER SYSTEM
  // ─────────────────────────────────────────────────────────────

  let _banCache = null;        // { blocked: 0|1, ... }
  let _banCheckPromise = null;

  /**
   * Получает статус бана пользователя.
   * Кэширует результат на время сессии.
   */
  window.checkUserBan = async function checkUserBan(force = false) {
    if (!force && _banCache !== null) {
      return _banCache;
    }
    if (_banCheckPromise) {
      return _banCheckPromise;
    }

    const tg = window.Telegram?.WebApp;
    const userId = tg?.initDataUnsafe?.user?.id;

    if (!userId) {
      return { blocked: 0 };
    }

    _banCheckPromise = (async () => {
      try {
        const api = window.apiFetch || fetch;
        const res = await api(`/api/user/flags?user_id=${userId}`);
        if (!res.ok) throw new Error('flags fetch failed');
        const data = await res.json();
        _banCache = data;
        return data;
      } catch (e) {
        // В случае ошибки не баним пользователя (fail-open для удобства)
        console.warn('[BAN] Не удалось проверить статус блокировки', e);
        _banCache = { blocked: 0 };
        return _banCache;
      } finally {
        _banCheckPromise = null;
      }
    })();

    return _banCheckPromise;
  };

  /**
   * Показывает большое модальное окно с сообщением о бане.
   * Блокирует взаимодействие с играми.
   */
  window.showBanOverlay = function showBanOverlay() {
    // Если оверлей уже есть — не дублируем
    if (document.getElementById('ban-overlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'ban-overlay';
    overlay.style.cssText = `
      position: fixed;
      inset: 0;
      z-index: 999999;
      background: rgba(10, 5, 25, 0.96);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    `;

    overlay.innerHTML = `
      <div style="
        max-width: 340px;
        background: linear-gradient(145deg, #1a103d, #120b2e);
        border: 1px solid #ff4466;
        border-radius: 20px;
        padding: 28px 22px;
        text-align: center;
        box-shadow: 0 0 60px rgba(255, 68, 102, 0.3);
        font-family: 'Rajdhani', sans-serif;
      ">
        <div style="font-size: 48px; margin-bottom: 12px; filter: drop-shadow(0 0 8px #ff4466);">🚫</div>
        
        <div style="
          font-family: 'Orbitron', sans-serif;
          font-size: 18px;
          font-weight: 700;
          color: #ff4466;
          text-shadow: 0 0 12px rgba(255, 68, 102, 0.6);
          margin-bottom: 16px;
          line-height: 1.3;
        ">
          Доступ к играм заблокирован
        </div>
        
        <div style="
          font-size: 15.5px;
          line-height: 1.45;
          color: rgba(255,255,255,0.92);
          margin-bottom: 26px;
        ">
          Вы себя плохо вели и нарушали правила проекта,<br>
          поэтому игры вам больше недоступны.
        </div>
        
        <button id="ban-close-btn" style="
          background: rgba(255,68,102,0.15);
          color: #ff4466;
          border: 1px solid #ff4466;
          border-radius: 999px;
          padding: 10px 32px;
          font-family: 'Orbitron', sans-serif;
          font-size: 13px;
          letter-spacing: 0.5px;
          cursor: pointer;
        ">Понятно</button>
      </div>
    `;

    document.body.appendChild(overlay);

    // Кнопка просто закрывает оверлей (пользователь всё равно не сможет играть)
    const btn = overlay.querySelector('#ban-close-btn');
    if (btn) {
      btn.onclick = () => overlay.remove();
    }

    // Дополнительно: клик по фону тоже закрывает
    overlay.onclick = (e) => {
      if (e.target === overlay) overlay.remove();
    };
  };

  /**
   * Проверяет бан и при необходимости показывает оверлей.
   * Возвращает true, если пользователь забанен.
   */
  window.enforceBanIfNeeded = async function enforceBanIfNeeded() {
    const flags = await window.checkUserBan();
    if (flags && flags.blocked) {
      window.showBanOverlay();
      return true;
    }
    return false;
  };

  // ─────────────────────────────────────────────────────────────
  // ДИНАМИЧЕСКАЯ РЕАКЦИЯ НА ИЗМЕНЕНИЕ СТАТУСА БАНА
  // ─────────────────────────────────────────────────────────────

  let _previousBlocked = null;
  let _banWatcherInterval = null;

  /**
   * Запускает периодическую проверку статуса бана.
   * При изменении статуса (разбан / бан) реагирует автоматически:
   *   - Если забанили → показываем оверлей и блокируем игры.
   *   - Если разбанили → скрываем оверлей (если был) и рекомендуем перезагрузку страницы.
   *
   * Вызывать один раз на странице (например после готовности tg).
   */
  window.startBanWatcher = function startBanWatcher(intervalMs = 45000) {
    if (_banWatcherInterval) return; // уже запущен

    _banWatcherInterval = setInterval(async () => {
      try {
        const flags = await window.checkUserBan(true); // force refresh
        const isBlocked = !!(flags && flags.blocked);

        if (_previousBlocked === null) {
          _previousBlocked = isBlocked;
          return;
        }

        if (isBlocked !== _previousBlocked) {
          _previousBlocked = isBlocked;

          if (isBlocked) {
            // Пользователя только что забанили
            console.log('[BAN] Пользователь заблокирован (динамически)');
            window.showBanOverlay();

            // Дополнительно пытаемся отключить игровые элементы на текущей странице
            if (typeof window.applyGlobalBanRestrictions === 'function') {
              window.applyGlobalBanRestrictions();
            }
          } else {
            // Пользователя разбанили
            console.log('[BAN] Пользователь разблокирован (динамически)');
            const overlay = document.getElementById('ban-overlay');
            if (overlay) overlay.remove();

            // Самый чистый вариант — мягко перезагрузить страницу,
            // чтобы все ограничения (disabled карточки и т.д.) снялись.
            // Делаем это с небольшой задержкой, чтобы пользователь увидел уведомление.
            setTimeout(() => {
              try {
                window.location.reload();
              } catch (_) {}
            }, 1200);
          }
        }
      } catch (e) {
        // тихо игнорируем ошибки watcher'а
      }
    }, intervalMs);
  };

  /**
   * Отключает пункт навигации «Игры» в нижней панели (если есть).
   * Используется на страницах Профиль и Магазин.
   */
  window.disableGamesNavIfBanned = async function disableGamesNavIfBanned() {
    const flags = await window.checkUserBan();
    if (!flags || !flags.blocked) return;

    // Ищем элементы навигации, ведущие на игры (index.html)
    const navItems = document.querySelectorAll('.chin-navbar__item');

    navItems.forEach(item => {
      const href = item.getAttribute('href') || '';
      const text = (item.textContent || '').trim().toLowerCase();

      const isGamesLink = href.includes('index.html') ||
                          href === 'index.html' ||
                          text.includes('игры') ||
                          text.includes('игр');

      if (isGamesLink) {
        // Визуально отключаем
        item.style.opacity = '0.35';
        item.style.pointerEvents = 'auto';
        item.style.filter = 'grayscale(0.7)';

        // Перехватываем клик
        item.onclick = (e) => {
          e.preventDefault();
          if (window.showBanOverlay) window.showBanOverlay();
          else if (window.enforceBanIfNeeded) window.enforceBanIfNeeded();
        };

        // Добавляем класс для возможной стилизации
        item.classList.add('nav-banned');
      }
    });
  };

  /**
   * Универсальная функция для применения ограничений на текущей странице.
   * Может вызываться из watcher'а.
   */
  window.applyGlobalBanRestrictions = function applyGlobalBanRestrictions() {
    // Отключаем карточки игр на главной (если мы на index.html)
    document.querySelectorAll('.game-card').forEach(card => {
      card.style.opacity = '0.4';
      card.style.pointerEvents = 'auto';
      card.style.filter = 'grayscale(0.8)';

      card.onclick = (e) => {
        e.preventDefault();
        if (window.showBanOverlay) window.showBanOverlay();
      };
    });

    // Отключаем навигацию "Игры" (на случай, если мы на профиле/магазине)
    const navItems = document.querySelectorAll('.chin-navbar__item');
    navItems.forEach(item => {
      const href = item.getAttribute('href') || '';
      const text = (item.textContent || '').toLowerCase();
      if (href.includes('index.html') || text.includes('игры')) {
        item.style.opacity = '0.35';
        item.style.pointerEvents = 'auto';
        item.style.filter = 'grayscale(0.7)';
        item.onclick = (e) => {
          e.preventDefault();
          if (window.showBanOverlay) window.showBanOverlay();
        };
      }
    });
  };
})();
