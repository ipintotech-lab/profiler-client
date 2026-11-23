/* The page's theme: light (blue on cream) or dark (the Openly7 wordmark's navy and indigo). report.css holds one
 * set of variables per theme, picked by data-theme on <html>; a page that never loads this stays light.
 *
 * OpenlyTheme.init() sets data-theme from the reader's saved choice, else prefers-color-scheme (followed live while
 * nothing is saved). OpenlyTheme.toggle() makes the sun/moon button that switches it and saves the choice.
 * The one thing kept in localStorage is that choice ("light" or "dark"); report.js, which holds the names,
 * stores nothing.
 */
(function () {
'use strict';
// ---- the theme: light (blue on cream) or dark (Openly7 navy and indigo), one set of variables each in report.css.
// The reader's choice is kept in localStorage; without one the page follows prefers-color-scheme, live. Storage
// can be missing or throw (a file:// page, a locked-down browser): the theme still works for the page's life.
const THEME_KEY = 'openly7-theme', THEME_EVENT = 'openly7-theme';  // report.js listens for THEME_EVENT
let chosen = null;  // this page's choice, kept even where storage is refused
const themeStored = () => { try { const v = globalThis.localStorage?.getItem(THEME_KEY); return v === 'light' || v === 'dark' ? v : null; } catch (e) { return null; } };
const themeSystem = () => typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
const theme = {
  get: () => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light',
  set(v, remember = true) {
    document.documentElement.dataset.theme = v === 'dark' ? 'dark' : 'light';
    if (remember) { chosen = theme.get(); try { globalThis.localStorage?.setItem(THEME_KEY, theme.get()); } catch (e) { /* not kept: fine */ } }
    document.dispatchEvent(new Event(THEME_EVENT));
  },
  init() {
    theme.set(themeStored() || themeSystem(), false);  // before the report is drawn, so it never flashes
    if (typeof matchMedia === 'function') {
      const mq = matchMedia('(prefers-color-scheme: dark)'), follow = () => { if (!themeStored() && !chosen) theme.set(themeSystem(), false); };
      mq.addEventListener ? mq.addEventListener('change', follow) : mq.addListener?.(follow);
    }
    return theme.get();
  },
};
const SUN = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5"></circle><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"></path></svg>';
const MOON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"></path></svg>';
// the button: a moon in the light theme (switch to dark), a sun in the dark; `className` is the host's button class
function themeToggle({ className = 'btn' } = {}) {
  const b = document.createElement('button');
  b.type = 'button'; b.className = `${className} theme-toggle`;
  const show = () => { const dark = theme.get() === 'dark', label = `Switch to ${dark ? 'light' : 'dark'} theme`;
    b.innerHTML = dark ? SUN : MOON; b.setAttribute('aria-label', label); b.title = label; };
  b.addEventListener('click', () => theme.set(theme.get() === 'dark' ? 'light' : 'dark'));
  document.addEventListener(THEME_EVENT, show);
  show();
  return b;
}

window.OpenlyTheme = { init: theme.init, get: theme.get, set: theme.set, toggle: themeToggle, KEY: THEME_KEY, EVENT: THEME_EVENT };
})();
