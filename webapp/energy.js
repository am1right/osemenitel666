/**
 * ⚡ CHIN GAMES — POWER SYSTEM (energy.js)
 *
 * Подключи этот файл на всех страницах перед закрывающим </body>:
 *   <script src="../energy.js"></script>
 *
 * API:
 *   Energy.get()            → { current, max, nextRechargeIn }
 *   Energy.canPlay(cost)    → true / false
 *   Energy.spend(cost)      → true (потрачено) / false (не хватает)
 *   Energy.showWidget()     → рендерит виджет в #energy-widget
 *   Energy.init(cost)       → монтирует виджет + блокирует кнопку старта если нет энергии
 *
 * Overflow-логика (покупки в магазине):
 *   Пользователь может иметь БОЛЬШЕ MAX энергии после покупки.
 *   Регенерация НЕ работает пока current > MAX.
 *   Виджет визуально выделяет overflow золотым цветом.
 */

const Energy = (() => {
    // ─── CONFIG ───────────────────────────────────────────
    const MAX         = 8;               // базовый максимум (= бэкенд MAX)
    const REGEN_MS    = 12 * 60 * 1000;  // 12 минут на единицу
    const STORAGE_KEY = 'cg_energy_v1';
    const API         = 'https://chingames.duckdns.org';

    function _iconSrc() {
        return location.pathname.includes('/games/')
            ? '../icons/enegry.png'
            : 'icons/enegry.png';
    }

    function iconHtml(sizePx = 20) {
        return `<img src="${_iconSrc()}" alt="" class="cg-energy-icon" width="${sizePx}" height="${sizePx}" style="width:${sizePx}px;height:${sizePx}px;object-fit:contain;display:block;filter:drop-shadow(0 0 6px rgba(0,255,255,0.8));">`;
    }

    // ─── PERSISTENCE ──────────────────────────────────────
    function _load() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) return { amount: MAX, lastRegen: Date.now() };
            return JSON.parse(raw);
        } catch {
            return { amount: MAX, lastRegen: Date.now() };
        }
    }

    function _save(state) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }

    // ─── REGEN ENGINE ─────────────────────────────────────
    // Регенерация срабатывает только если current < MAX.
    // Если current > MAX (overflow от покупки) — таймер заморожен,
    // регенерация начнётся когда текущий запас упадёт ниже MAX.
    function _applyRegen(state) {
        if (state.amount >= MAX) {
            // Обновляем lastRegen чтобы таймер не «накапливал» время
            state.lastRegen = Date.now();
            return state;
        }
        const now     = Date.now();
        const elapsed = now - state.lastRegen;
        const gained  = Math.floor(elapsed / REGEN_MS);

        if (gained > 0) {
            state.amount    = Math.min(MAX, state.amount + gained);
            state.lastRegen = state.lastRegen + gained * REGEN_MS;
            if (state.amount >= MAX) state.lastRegen = now;
        }
        return state;
    }

    // ─── PUBLIC API ───────────────────────────────────────
    function get() {
        let s = _applyRegen(_load());
        _save(s);

        let nextRechargeIn = null;
        if (s.amount < MAX) {
            const elapsed = Date.now() - s.lastRegen;
            nextRechargeIn = Math.max(0, REGEN_MS - elapsed);
        }

        // overflow = true когда энергии больше базового максимума
        const overflow = s.amount > MAX;

        return { current: s.amount, max: MAX, nextRechargeIn, overflow };
    }

    function canPlay(cost = 1) {
        return get().current >= cost;
    }

    function _getUserId() {
        return window.Telegram?.WebApp?.initDataUnsafe?.user?.id ?? null;
    }

    /** Синхронизация энергии с сервером (после админ-начисления и при загрузке). */
    async function pull() {
        const userId = _getUserId();
        if (!userId) return null;
        try {
            const api = window.apiFetch || fetch;
            const res = await api(`${API}/api/energy/balance?user_id=${userId}`);
            if (!res.ok) return null;
            const d = await res.json();
            _save({ amount: d.amount, lastRegen: d.last_regen });
            return d;
        } catch {
            return null;
        }
    }

    function spend(cost = 1) {
        const userId = _getUserId();
        if (userId) {
            const api = window.apiFetch || fetch;
            api(`${API}/api/energy/spend`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId, cost }),
            }).then(r => r.ok ? r.json() : null).then(d => {
                if (d && d.amount != null) {
                    _save({ amount: d.amount, lastRegen: d.last_regen });
                }
            }).catch(() => {});
        }

        let s = _applyRegen(_load());
        if (s.amount < cost) { _save(s); return false; }
        s.amount -= cost;
        if (s.amount < MAX) {
            if (s.lastRegen <= Date.now() - REGEN_MS) {
                s.lastRegen = Date.now();
            }
        }
        _save(s);
        return true;
    }

    // ─── UI WIDGET ────────────────────────────────────────
    const WIDGET_CSS = `
        #energy-widget {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0,0,0,0.55);
            border: 1px solid rgba(0,255,255,0.35);
            border-radius: 30px;
            padding: 7px 14px;
            backdrop-filter: blur(8px);
            box-shadow: 0 0 12px rgba(0,255,255,0.15);
            font-family: 'Orbitron', sans-serif;
            font-size: 13px;
            color: #fff;
            min-width: 160px;
            position: relative;
            transition: border-color 0.4s, box-shadow 0.4s;
        }
        #energy-widget .ew-label {
            display: flex;
            align-items: center;
            flex-shrink: 0;
        }
        .cg-energy-icon { vertical-align: middle; }
        #energy-widget .ew-bar-wrap {
            flex: 1;
            height: 8px;
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
            overflow: hidden;
            position: relative;
        }
        #energy-widget .ew-bar-fill {
            height: 100%;
            border-radius: 4px;
            background: linear-gradient(90deg, #00ffff, #bc13fe);
            box-shadow: 0 0 8px #00ffff;
            transition: width 0.5s ease, background 0.4s;
        }
        #energy-widget .ew-count {
            font-size: 13px;
            font-weight: 700;
            color: #fff;
            white-space: nowrap;
        }
        #energy-widget .ew-timer {
            font-size: 10px;
            color: rgba(255,255,255,0.5);
            white-space: nowrap;
        }

        /* ── EMPTY STATE ── */
        #energy-widget.ew-empty {
            border-color: rgba(255,0,100,0.5);
            box-shadow: 0 0 12px rgba(255,0,100,0.2);
        }
        #energy-widget.ew-empty .ew-bar-fill {
            background: linear-gradient(90deg, #ff0055, #ff4400);
            box-shadow: 0 0 8px #ff0055;
        }
        #energy-widget.ew-empty .ew-count {
            color: #ff4466;
            text-shadow: 0 0 6px #ff0055;
        }

        /* ── OVERFLOW STATE (куплено > MAX) ── */
        #energy-widget.ew-overflow {
            border-color: rgba(255,215,0,0.7);
            box-shadow: 0 0 16px rgba(255,215,0,0.3);
            animation: overflowPulse 2s ease-in-out infinite;
        }
        #energy-widget.ew-overflow .ew-bar-fill {
            background: linear-gradient(90deg, #ffd700, #ff9900);
            box-shadow: 0 0 10px rgba(255,215,0,0.8);
        }
        #energy-widget.ew-overflow .ew-count {
            color: #ffd700;
            text-shadow: 0 0 8px rgba(255,215,0,0.8);
        }
        #energy-widget.ew-overflow .ew-label {
            color: #ffd700;
            text-shadow: 0 0 8px #ffd700;
        }
        /* Полоска «переполнения» поверх бара — маленький золотой штрих справа */
        #energy-widget.ew-overflow .ew-bar-wrap::after {
            content: '';
            position: absolute;
            right: 0; top: 0; bottom: 0;
            width: 4px;
            background: #fff;
            border-radius: 0 4px 4px 0;
            box-shadow: 0 0 6px #ffd700;
            animation: overflowTick 1s ease-in-out infinite alternate;
        }
        @keyframes overflowPulse {
            0%, 100% { box-shadow: 0 0 14px rgba(255,215,0,0.25); border-color: rgba(255,215,0,0.6); }
            50%       { box-shadow: 0 0 28px rgba(255,215,0,0.5);  border-color: rgba(255,215,0,0.9); }
        }
        @keyframes overflowTick {
            from { opacity: 0.6; transform: scaleY(0.7); }
            to   { opacity: 1;   transform: scaleY(1); }
        }

        /* Оверлей «нет энергии» */
        #no-energy-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(42,16,80,0.85);
            backdrop-filter: blur(6px);
            z-index: 999;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 16px;
            font-family: 'Orbitron', sans-serif;
            text-align: center;
            padding: 30px;
        }
        #no-energy-overlay.show { display: flex; }
        #no-energy-overlay .neo-icon {
            display: flex;
            justify-content: center;
            animation: pulse-neo 1.5s infinite alternate;
        }
        #no-energy-overlay .neo-icon .cg-energy-icon {
            width: 56px;
            height: 56px;
            filter: drop-shadow(0 0 14px rgba(0,255,255,0.9));
        }
        #no-energy-overlay h2 {
            font-size: 22px;
            color: #fff;
            text-shadow: 0 0 10px #ff0055;
            margin: 0;
        }
        #no-energy-overlay p {
            color: rgba(255,255,255,0.6);
            font-family: 'Rajdhani', sans-serif;
            font-size: 16px;
            margin: 0;
        }
        #no-energy-overlay .neo-timer {
            font-size: 26px;
            color: #00ffff;
            text-shadow: 0 0 12px #00ffff;
        }
        #no-energy-overlay .neo-back {
            margin-top: 10px;
            background: rgba(0,0,0,0.6);
            border: 2px solid #00ffff;
            color: #00ffff;
            padding: 12px 28px;
            border-radius: 30px;
            font-family: 'Orbitron', sans-serif;
            font-size: 14px;
            cursor: pointer;
            box-shadow: 0 0 10px rgba(0,255,255,0.3);
        }
        @keyframes pulse-neo {
            from { filter: drop-shadow(0 0 6px #ff0055); }
            to   { filter: drop-shadow(0 0 20px #ff0055) drop-shadow(0 0 40px #ff0055); }
        }
    `;

    function _injectCSS() {
        if (document.getElementById('energy-widget-css')) return;
        const style = document.createElement('style');
        style.id = 'energy-widget-css';
        style.textContent = WIDGET_CSS;
        document.head.appendChild(style);
    }

    function _fmtTime(ms) {
        const totalSec = Math.ceil(ms / 1000);
        const m = Math.floor(totalSec / 60);
        const s = totalSec % 60;
        return `${m}:${s.toString().padStart(2,'0')}`;
    }

    // Монтирует #energy-widget в переданный контейнер (или создаёт поверх экрана)
    function showWidget(containerEl) {
        _injectCSS();

        let wrap = document.getElementById('energy-widget');
        if (!wrap) {
            wrap = document.createElement('div');
            wrap.id = 'energy-widget';
            (containerEl || document.body).appendChild(wrap);
        }

        function refresh() {
            const { current, max, nextRechargeIn, overflow } = get();
            const empty = current === 0;

            // Overflow: бар всегда 100%, счётчик показывает реальное число
            // Normal:   бар пропорционален current/max
            const pct = overflow ? 100 : (current / max) * 100;

            // Состояния: ew-overflow > ew-empty > '' (нормальное)
            wrap.className = overflow ? 'ew-overflow' : (empty ? 'ew-empty' : '');

            // Таймер: при overflow показываем подсказку что реген заморожен
            let timerHtml = '';
            if (overflow) {
                timerHtml = `<span class="ew-timer" style="color:rgba(255,215,0,0.6)">реген заморожен</span>`;
            } else if (nextRechargeIn !== null) {
                timerHtml = `<span class="ew-timer">+1 через ${_fmtTime(nextRechargeIn)}</span>`;
            }

            wrap.innerHTML = `
                <span class="ew-label">${iconHtml(22)}</span>
                <div class="ew-bar-wrap">
                    <div class="ew-bar-fill" style="width:${pct}%"></div>
                </div>
                <span class="ew-count">${current}/${max}</span>
                ${timerHtml}
            `;
        }

        refresh();
        const iv = setInterval(refresh, 1000);
        wrap._energyInterval = iv;

        return wrap;
    }

    // ─── NO-ENERGY OVERLAY ───────────────────────────────
    function _showNoEnergyOverlay() {
        _injectCSS();

        let overlay = document.getElementById('no-energy-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'no-energy-overlay';
            document.body.appendChild(overlay);
        }

        function renderOverlay() {
            const { nextRechargeIn } = get();
            overlay.innerHTML = `
                <div class="neo-icon">${iconHtml(56)}</div>
                <h2>POWER EXHAUSTED</h2>
                <p>Энергия закончилась.<br>Восстановление автоматическое.</p>
                <div class="neo-timer">${nextRechargeIn !== null ? _fmtTime(nextRechargeIn) : '—'}</div>
                <button class="neo-back" onclick="window.location.href='../index.html'">← Главное меню</button>
            `;
        }

        renderOverlay();
        overlay.classList.add('show');

        const iv = setInterval(() => {
            const { current } = get();
            if (current > 0) {
                overlay.classList.remove('show');
                clearInterval(iv);
            } else {
                renderOverlay();
            }
        }, 1000);
    }

    // ─── INIT для игровых страниц ─────────────────────────
    /**
     * init(cost, startBtnId, widgetContainerId)
     *
     * cost              — сколько энергии тратит эта игра
     * startBtnId        — id кнопки «Играть» / «Старт»  (если есть)
     * widgetContainerId — id блока куда встроить виджет (опционально)
     *
     * Возвращает функцию-хук: вызови её перед стартом игры.
     * Если энергии нет — показывает оверлей и возвращает false.
     */
    function init(cost = 1, startBtnId = null, widgetContainerId = null) {
        _injectCSS();

        const containerEl = widgetContainerId
            ? document.getElementById(widgetContainerId)
            : null;

        showWidget(containerEl);
        pull().then(() => {
            showWidget(containerEl);
            if (startBtnId) _updateBtn(startBtnId, cost);
        });

        if (startBtnId) {
            _updateBtn(startBtnId, cost);
        }

        return function trySpend() {
            if (spend(cost)) {
                showWidget(containerEl);
                return true;
            }
            _showNoEnergyOverlay();
            return false;
        };
    }

    function _updateBtn(btnId, cost) {
        const btn = document.getElementById(btnId);
        if (!btn) return;
        const { current } = get();
        if (current < cost) {
            btn.disabled = true;
            btn.style.opacity = '0.45';
            btn.style.cursor  = 'not-allowed';
            btn.title = 'Недостаточно энергии';
        } else {
            btn.disabled = false;
            btn.style.opacity = '';
            btn.style.cursor  = '';
            btn.title = '';
        }
    }

    return { get, canPlay, spend, showWidget, init, pull, MAX, API, iconHtml, iconSrc: _iconSrc };
})();
