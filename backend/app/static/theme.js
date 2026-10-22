(() => {
  const key = 'jobharbor-theme';
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
      button.setAttribute('aria-pressed', String(theme === 'dark'));
      const label = button.querySelector('[data-theme-label]');
      if (label) label.textContent = theme === 'dark' ? 'Dark' : 'Light';
    });
  };
  let current = 'light';
  try { current = localStorage.getItem(key) === 'dark' ? 'dark' : 'light'; } catch {}
  apply(current);
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-theme-toggle]');
    if (!button) return;
    current = current === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(key, current); } catch {}
    apply(current);
  });
})();
