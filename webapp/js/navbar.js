/* Общий нижний navbar для всех страниц webapp */
(function () {
    const path = location.pathname.replace(/\\/g, '/');
    const inGames = /\/games\//.test(path);
    const prefix = inGames ? '../' : '';

    function isActive(name) {
        if (name === 'games') return /\/games\//.test(path) || /index\.html$/.test(path) || /\/$/.test(path);
        if (name === 'profile') return /profile\.html$/.test(path);
        if (name === 'shop') return /shop\.html$/.test(path);
        if (name === 'cases') return /cases\.html$/.test(path);
        return false;
    }

    function cls(name) {
        return 'chin-navbar__item' + (isActive(name) ? ' chin-navbar__item--active' : '');
    }

    const html = `
    <nav class="chin-navbar">
        <a class="${cls('games')}" href="${prefix}index.html">
            <span class="chin-navbar__icon"><img src="${prefix}icons/games.png" alt="Игры" style="object-fit:contain;filter:drop-shadow(0 0 5px rgba(255,255,255,0.3));"></span>
            <span>Игры</span>
        </a>
        <a class="${cls('profile')}" href="${prefix}profile.html">
            <span class="chin-navbar__icon"><img src="${prefix}icons/profile.png" alt="Профиль" style="object-fit:contain;filter:drop-shadow(0 0 5px rgba(255,255,255,0.3));"></span>
            <span>Профиль</span>
        </a>
        <a class="${cls('shop')}" href="${prefix}shop.html">
            <span class="chin-navbar__icon"><img src="${prefix}icons/shop.png" alt="Магазин" style="object-fit:contain;filter:drop-shadow(0 0 5px rgba(255,255,255,0.3));"></span>
            <span>Магазин</span>
        </a>
        <a class="${cls('cases')}" href="${prefix}cases.html">
            <span class="chin-navbar__icon"><img src="${prefix}icons/box.webp" alt="Кейсы" style="object-fit:contain;filter:drop-shadow(0 0 5px rgba(255,255,255,0.3));"></span>
            <span>Кейсы</span>
        </a>
    </nav>`;

    document.addEventListener('DOMContentLoaded', () => {
        const mount = document.getElementById('chin-navbar-mount');
        if (mount) mount.outerHTML = html;
        else document.body.insertAdjacentHTML('beforeend', html);
    });
})();
