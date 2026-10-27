// Interface language. Source strings are Russian and double as keys: tr('В очереди') returns the
// translation from /static/locales/<code>.json or the source string when none exists.
// Placeholders use braces: tr('Ждут: {n}', {n: 3}). Static markup in index.html is translated by
// text node; an element marked data-i18n-html is translated as a whole, keeping its inline tags.
// The locale loads synchronously so module-level constants in later scripts are already translated.
const LANGUAGES = [
  ['en', 'English'], ['zh', '中文'], ['hi', 'हिन्दी'], ['es', 'Español'], ['ar', 'العربية'],
  ['fr', 'Français'], ['bn', 'বাংলা'], ['pt', 'Português'], ['ru', 'Русский'], ['ja', '日本語'],
];
const LOCALE_VERSION = '20261027-seed';
const LANG = (() => {
  let stored = null;
  try { stored = localStorage.getItem('df-lang'); } catch {}
  const browser = (navigator.languages || [navigator.language || '']).map(l => l.toLowerCase().split('-')[0]);
  return [stored, ...browser, 'en'].find(code => LANGUAGES.some(([c]) => c === code));
})();
// BCP 47 tag for dates, numbers and sorting.
const LOCALE = {en: 'en-US', zh: 'zh-CN', hi: 'hi-IN', es: 'es-ES', ar: 'ar', fr: 'fr-FR', bn: 'bn-BD', pt: 'pt-BR', ru: 'ru-RU', ja: 'ja-JP'}[LANG];
const MESSAGES = (() => {
  if (LANG === 'ru') return {};
  try {
    const request = new XMLHttpRequest();
    request.open('GET', `/static/locales/${LANG}.json?v=${LOCALE_VERSION}`, false);
    request.send();
    return request.status === 200 ? JSON.parse(request.responseText) : {};
  } catch { return {}; }
})();

function tr(source, vars) {
  const text = MESSAGES[source] || source;
  return vars ? text.replace(/\{(\w+)\}/g, (match, key) => key in vars ? vars[key] : match) : text;
}

(() => {
  const root = document.documentElement;
  root.lang = LANG;
  root.dir = LANG === 'ar' ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n-html]').forEach(el => { el.innerHTML = tr(el.innerHTML.trim()); });
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue.trim();
    if (text && MESSAGES[text]) node.nodeValue = node.nodeValue.replace(text, MESSAGES[text]);
  }
  document.querySelectorAll('[title],[placeholder],[aria-label]').forEach(el => {
    for (const name of ['title', 'placeholder', 'aria-label']) if (el.hasAttribute(name)) el.setAttribute(name, tr(el.getAttribute(name)));
  });
  // The same picker sits on the login card and in the sidebar footer; a change reloads the page.
  document.querySelectorAll('[data-language-picker]').forEach(slot => {
    const select = document.createElement('select');
    select.className = 'language-picker';
    select.setAttribute('aria-label', 'Language');
    select.innerHTML = LANGUAGES.map(([code, name]) => `<option value="${code}" ${code === LANG ? 'selected' : ''}>${name}</option>`).join('');
    select.onchange = () => { try { localStorage.setItem('df-lang', select.value); } catch {} location.reload(); };
    slot.append(select);
  });
})();
