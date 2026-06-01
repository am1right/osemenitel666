const tg = window.Telegram.WebApp;

// ===================== ЗАЩИТА MINI APP =====================
tg.ready();
tg.enableClosingConfirmation();       // Просит подтверждение при попытке закрыть

// Полноэкранный режим ТОЛЬКО на телефоне
if (typeof window.initTelegramFullscreen === 'function') {
  window.initTelegramFullscreen();
} else {
  // Fallback, если api.js ещё не подключён
  tg.expand();
}

// Максимальная защита от свайпа вниз и других жестов
document.documentElement.style.overscrollBehavior = 'none';
document.body.style.overscrollBehavior = 'none';
document.body.style.touchAction = 'none';
document.body.style.overflow = 'hidden';
document.body.style.position = 'fixed';
document.body.style.width = '100%';
document.body.style.height = '100%';

// Жёсткая блокировка свайпа вниз
let touchStartY = 0;
document.addEventListener('touchstart', e => {
    touchStartY = e.touches[0].clientY;
}, { passive: true });

document.addEventListener('touchmove', e => {
    const currentY = e.touches[0].clientY;
    // Если свайп вниз более 50px — блокируем
    if (currentY - touchStartY > 50) {
        e.preventDefault();
        e.stopImmediatePropagation();
    }
}, { passive: false });

// Блокировка double-tap zoom
let lastTouchEnd = 0;
document.addEventListener('touchend', e => {
    const now = Date.now();
    if (now - lastTouchEnd <= 300) {
        e.preventDefault();
    }
    lastTouchEnd = now;
}, false);

// ===================== ОСНОВНЫЕ ФУНКЦИИ =====================
function showScreen(screenId) {
    // Скрываем все экраны
    document.querySelectorAll('.screen').forEach(el => {
        el.classList.remove('active');
        el.classList.add('hidden');
    });
    
    // Показываем нужный
    const target = document.getElementById(screenId);
    if (target) {
        target.classList.remove('hidden');
        target.classList.add('active');
        
        // Вибрация при переходе
        if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    }
}

function launchGame(url) {
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
    window.location.href = url;
}

async function loadLeaderboard(gameName) {
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
    
    const titleMap = {
        'math': '🧮 Math Master',
        '2048': '🎮 2048',
        'snake': '🐍 Snake',
        'flappy': '🐦 Flappy Chin'
    };
    
    document.getElementById('lb-game-title').innerText = titleMap[gameName] || gameName;
    showScreen('leaderboard-view');
    
    const tbody = document.getElementById('lb-body');
    tbody.innerHTML = '<tr><td colspan="3" class="loading">Загрузка...</td></tr>';
    
    try {
        const api = window.apiFetch || fetch;
        const response = await api(`/api/leaderboard/${gameName}`);
        const data = await response.json();
        
        tbody.innerHTML = '';
        
        if (data.leaders && data.leaders.length > 0) {
            data.leaders.forEach((player, index) => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${index + 1}</td>
                    <td>${escapeHtml(player.first_name || player.username || 'Игрок')}</td>
                    <td style="color: var(--primary); font-weight:bold;">${player.score}</td>
                `;
                tbody.appendChild(row);
            });
        } else {
            tbody.innerHTML = '<tr><td colspan="3" class="loading">Пока нет игроков. Стань первым!</td></tr>';
        }
    } catch (error) {
        console.error('Error loading leaderboard:', error);
        tbody.innerHTML = '<tr><td colspan="3" class="loading">Ошибка загрузки</td></tr>';
    }
}

function escapeHtml(text) {
    if (!text) return 'Игрок';
    return text.toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}