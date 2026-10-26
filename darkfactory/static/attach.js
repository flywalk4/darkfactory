'use strict';
// Task attachments for modal forms: file picker, paste and drop; files go to the API as base64.
(() => {
  const FILE_BYTES = 10 * 1024 * 1024;
  const IMAGE = /^image\/(png|jpeg|gif|webp)$/;
  let files = [];

  const escape = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const size = bytes => bytes < 1024 * 1024 ? Math.max(1, Math.round(bytes / 1024)) + ' ' + tr('КБ') : (bytes / 1024 / 1024).toFixed(1) + ' ' + tr('МБ');

  function render() {
    const list = document.querySelector('.attach-list');
    if (!list) return;
    list.innerHTML = files.map((file, index) => `<span class="attach-item" title="${escape(file.name)}">${file.preview ? `<img src="${file.preview}" alt="">` : '<i>📄</i>'}<span>${escape(file.name)}<small>${size(file.size)}</small></span><button type="button" data-attach-remove="${index}" aria-label="${tr('Убрать')}">×</button></span>`).join('');
    const error = document.querySelector('.attach-error');
    if (error) error.textContent = '';
  }

  function add(list) {
    const error = document.querySelector('.attach-error');
    for (const file of list) {
      if (file.size > FILE_BYTES) { if (error) error.textContent = tr('{name}: больше 10 МБ',{name:file.name}); continue; }
      if (files.length >= 20) { if (error) error.textContent = tr('Не больше 20 файлов'); break; }
      const name = file.name || `screenshot-${files.length + 1}.${(file.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`;
      files.push({ file, name, size: file.size, preview: IMAGE.test(file.type) ? URL.createObjectURL(file) : '' });
    }
    render();
  }

  window.attachField = (label = tr('Файлы и скриншоты')) => {
    files.forEach(file => file.preview && URL.revokeObjectURL(file.preview));
    files = [];
    return `<div class="attach-zone"><span class="attach-label">${escape(label)}</span><div class="attach-list"></div><label class="attach-button">📎 ${tr('Прикрепить')}<input type="file" class="attach-input" multiple hidden></label><small>${tr('Можно вставить скриншот через ⌘V / Ctrl+V или перетащить файлы. До 10 МБ на файл.')}</small><p class="attach-error"></p></div>`;
  };

  window.takeAttachments = () => Promise.all(files.map(({ file, name }) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name, media_type: file.type || 'application/octet-stream', data: String(reader.result).split(',', 2)[1] || '' });
    reader.onerror = () => reject(new Error(tr('Не удалось прочитать {name}',{name})));
    reader.readAsDataURL(file);
  })));

  document.addEventListener('change', event => {
    if (!event.target.matches('.attach-input')) return;
    add(event.target.files);
    event.target.value = '';
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-attach-remove]');
    if (!button) return;
    const [removed] = files.splice(Number(button.dataset.attachRemove), 1);
    if (removed?.preview) URL.revokeObjectURL(removed.preview);
    render();
  });
  document.addEventListener('paste', event => {
    if (!document.querySelector('#modal[open] .attach-zone')) return;
    const pasted = Array.from(event.clipboardData?.files || []);
    if (!pasted.length) return;
    event.preventDefault();
    add(pasted);
  });
  document.addEventListener('dragover', event => {
    if (event.target.closest?.('#modal[open]') && document.querySelector('#modal[open] .attach-zone')) event.preventDefault();
  });
  document.addEventListener('drop', event => {
    if (!document.querySelector('#modal[open] .attach-zone') || !event.target.closest?.('#modal[open]')) return;
    event.preventDefault();
    add(event.dataTransfer?.files || []);
  });
})();
