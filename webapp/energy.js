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
    // Батарея 0..100%. Заряда хватает на ~50 мин игры; восстановление с 0 до
    // 100% ≈ 3 часа (база, ускоряется апгрейдом из магазина).
    const MAX         = 100;             // проценты (= бэкенд ENERGY_MAX)
    let   REGEN_MS    = 10 * 60 * 1000;  // 20 мин на 1% (база; сервер уточняет)
    const STORAGE_KEY = 'cg_energy_v1';
    const API         = 'https://chingames.duckdns.org';

    // Плавный расход: на входе списывается cost%, дальше пока идёт партия
    // батарея тает — DRAIN_UNIT_MS на 1%. 100% / (30с) ≈ 50 минут игры.
    // Кончилась посреди игры — даём доиграть, новый старт блокируется.
    const DRAIN_UNIT_MS = 7 * 1000;      // 1% за 7 секунд активной игры (~12 мин на заряд)
    const MIN_START     = 10;            // минимум % заряда, чтобы начать/продолжить партию

    // Состояние сессии расхода
    let _sessionOn   = false;
    let _drainTimer  = null;
    let _drainAccum  = 0;                 // мс, накопленные к следующей единице
    let _lastTick    = 0;

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
            if (d.regen_ms) REGEN_MS = d.regen_ms;   // учитываем апгрейд скорости регена
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
        // Любая трата сбрасывает таймер регена → во время игры не восстанавливается
        if (s.amount < MAX) s.lastRegen = Date.now();
        _save(s);
        return true;
    }

    // ─── DRAIN ENGINE (плавный расход во время игры) ──────
    // Доля до следующей списываемой единицы (0..1) — для плавной полоски.
    function _drainFraction() {
        return _sessionOn ? Math.min(1, _drainAccum / DRAIN_UNIT_MS) : 0;
    }

    function _refreshWidget() {
        const w = document.getElementById('energy-widget');
        if (w && typeof w._refresh === 'function') w._refresh();
    }

    function _drainTick() {
        const now = Date.now();
        _drainAccum += now - _lastTick;
        _lastTick = now;
        while (_drainAccum >= DRAIN_UNIT_MS) {
            if (get().current <= 0) { _drainAccum = 0; break; }  // пусто — доигрываем текущую
            spend(1);
            _drainAccum -= DRAIN_UNIT_MS;
        }
        _refreshWidget();
    }

    /** Старт сессии: запускает плавный расход (вызывается при старте партии). */
    function startSession() {
        if (_sessionOn) return;
        _sessionOn  = true;
        _drainAccum = 0;
        _lastTick   = Date.now();
        _drainTimer = setInterval(_drainTick, 250);
    }

    /** Конец сессии: останавливает расход (вызывается при game-over). */
    function endSession() {
        _sessionOn = false;
        if (_drainTimer) { clearInterval(_drainTimer); _drainTimer = null; }
        _drainAccum = 0;
        _refreshWidget();
    }

    // Подстраховка: вкладку свернули/закрыли — не тратим энергию «в фоне»
    document.addEventListener('visibilitychange', () => {
        if (document.hidden && _sessionOn && _drainTimer) {
            clearInterval(_drainTimer);
            _drainTimer = null;
        } else if (!document.hidden && _sessionOn && !_drainTimer) {
            _lastTick = Date.now();
            _drainTimer = setInterval(_drainTick, 250);
        }
    });

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
            transition: width 0.3s linear, background 0.4s;
        }
        #energy-widget .ew-count {
            font-size: 13px;
            font-weight: 700;
            color: #fff;
            white-space: nowrap;
        }
        /* ── Батарея телефона ── */
        #energy-widget .ew-battery {
            position: relative;
            flex: 1;
            height: 22px;
            min-width: 64px;
            border: 2px solid rgba(255,255,255,0.75);
            border-radius: 5px;
            overflow: hidden;
            background: rgba(255,255,255,0.06);
        }
        #energy-widget .ew-batt-fill {
            position: absolute; left: 0; top: 0; bottom: 0;
            border-radius: 2px;
            transition: width 0.3s linear, background 0.4s;
            background: linear-gradient(90deg, #00ffae, #00ffae);
            box-shadow: 0 0 10px rgba(0,255,174,0.6);
        }
        #energy-widget.ew-batt-low  .ew-batt-fill { background: #ffd93d; box-shadow: 0 0 10px rgba(255,217,61,0.6); }
        #energy-widget.ew-batt-crit .ew-batt-fill { background: #ff4466; box-shadow: 0 0 10px rgba(255,68,102,0.7); }
        #energy-widget.ew-batt-crit .ew-battery   { animation: battPulse 1.1s ease-in-out infinite; }
        @keyframes battPulse { 0%,100% { border-color: rgba(255,68,102,0.6);} 50% { border-color: rgba(255,68,102,1);} }
        #energy-widget .ew-batt-pct {
            position: absolute; inset: 0;
            display: flex; align-items: center; justify-content: center;
            font-family: 'Orbitron', sans-serif; font-size: 12px; font-weight: 700;
            color: #fff; text-shadow: 0 0 4px rgba(0,0,0,0.8); z-index: 1;
        }
        /* «Носик» батареи */
        #energy-widget .ew-batt-cap {
            width: 4px; height: 10px; flex-shrink: 0;
            background: rgba(255,255,255,0.75);
            border-radius: 0 2px 2px 0; margin-left: -2px;
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
            const { current, max, nextRechargeIn } = get();
            // Плавный расход: вычитаем дробную долю текущей сессии для «таяния»
            const display = Math.max(0, current - _drainFraction());
            const pct = Math.max(0, Math.min(100, (display / max) * 100));

            // Уровень заряда → цвет батареи
            const level = pct <= 15 ? 'crit' : (pct <= 40 ? 'low' : 'ok');
            wrap.className = `ew-batt-${level}` + (current === 0 ? ' ew-empty' : '');

            let timerHtml = '';
            if (current < max && nextRechargeIn !== null) {
                timerHtml = `<span class="ew-timer">+1% ${_fmtTime(nextRechargeIn)}</span>`;
            }

            wrap.innerHTML = `
                <div class="ew-battery">
                    <div class="ew-batt-fill" style="width:${pct}%"></div>
                    <span class="ew-batt-pct">${Math.round(display)}%</span>
                </div>
                <span class="ew-batt-cap"></span>
                ${timerHtml}
            `;

            // Кнопки «Продолжить» (.js-continue): если энергии мало — меняем подпись
            document.querySelectorAll('.js-continue').forEach((b) => {
                const ok = current >= MIN_START;
                b.disabled = false;   // если мало энергии — кнопка предложит докупить заряд
                b.textContent = ok ? 'Продолжить · 10 ⭐' : '⚡ Купить заряд · 32 ⭐';
            });
        }

        wrap._refresh = refresh;          // чтобы дренаж мог обновлять виджет на лету
        refresh();
        if (wrap._energyInterval) clearInterval(wrap._energyInterval);
        const iv = setInterval(refresh, 1000);
        wrap._energyInterval = iv;

        return wrap;
    }

    // ─── NO-ENERGY OVERLAY ───────────────────────────────
    function _showNoEnergyOverlay(reason) {
        _injectCSS();

        let overlay = document.getElementById('no-energy-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'no-energy-overlay';
            document.body.appendChild(overlay);
        }

        function renderOverlay() {
            const { nextRechargeIn } = get();
            const inGames = location.pathname.includes('/games/');
            const shopUrl = inGames ? '../shop.html' : 'shop.html';
            const menuUrl = inGames ? '../index.html' : 'index.html';
            const isMin = reason === 'min';
            const h2 = isMin ? `Нужно минимум ${MIN_START}%` : 'Энергия кончилась';
            const p = isMin
                ? `Для игры нужно минимум <b>${MIN_START}%</b> заряда.<br>Пополни или подожди восстановления.`
                : 'Доиграл — теперь самое интересное 😈<br>Пополни запас и продолжай, или подожди.';
            overlay.innerHTML = `
                <div class="neo-icon">${iconHtml(56)}</div>
                <h2>${h2}</h2>
                <p>${p}</p>
                <div class="neo-timer">+1 через ${nextRechargeIn !== null ? _fmtTime(nextRechargeIn) : '—'}</div>
                <button class="neo-back" style="border-color:#ffd700;color:#ffd700;box-shadow:0 0 12px rgba(255,215,0,0.35)"
                        onclick="window.location.href='${shopUrl}'">⚡ Пополнить</button>
                <button class="neo-back" onclick="window.location.href='${menuUrl}'">← Меню</button>
            `;
        }

        renderOverlay();
        overlay.classList.add('show');

        const threshold = reason === 'min' ? MIN_START : 1;
        const iv = setInterval(() => {
            const { current } = get();
            if (current >= threshold) {
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
            // Нужно минимум MIN_START% заряда, чтобы начать
            if (get().current < MIN_START) {
                _showNoEnergyOverlay('min');
                return false;
            }
            if (spend(cost)) {
                showWidget(containerEl);
                startSession();          // вход оплачен → запускаем плавный расход
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
        if (current < MIN_START) {
            btn.disabled = true;
            btn.style.opacity = '0.45';
            btn.style.cursor  = 'not-allowed';
            btn.title = `Нужно ≥${MIN_START}% заряда`;
        } else {
            btn.disabled = false;
            btn.style.opacity = '';
            btn.style.cursor  = '';
            btn.title = '';
        }
    }

    return { get, canPlay, spend, showWidget, init, pull, startSession, endSession,
             minStart: MIN_START, showNeedEnergy: () => _showNoEnergyOverlay('min'),
             MAX, API, iconHtml, iconSrc: _iconSrc };
})();
