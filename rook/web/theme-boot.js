// Applies this browser's last-used dashboard theme before the page paints,
// so a light or high-contrast theme never flashes dark first. A classic
// (blocking) script on purpose: it runs in <head>, after the stylesheets.
// The account's real choice arrives later (themes.js) and replaces it.
(function () {
  try {
    var saved = JSON.parse(localStorage.getItem('rook.theme') || 'null');
    if (!saved || typeof saved.css !== 'string' || !/^:root\{[^{}<>]*\}$/.test(saved.css)) return;
    var style = document.createElement('style');
    style.id = 'rook-theme';
    style.textContent = saved.css;
    document.head.appendChild(style);
    document.documentElement.setAttribute('data-theme', String(saved.id || '').replace(/[^a-z0-9-]/g, ''));
  } catch (e) { /* private mode or blocked storage: the default theme stays */ }
})();
