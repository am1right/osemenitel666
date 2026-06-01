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
})();
