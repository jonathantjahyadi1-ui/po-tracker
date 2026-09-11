document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.getElementById('menu-toggle');
  if (toggle) toggle.addEventListener('click', () => {
    const open = document.body.classList.toggle('nav-open');
    toggle.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && toggle) {
      document.body.classList.remove('nav-open');
      toggle.setAttribute('aria-expanded', 'false');
    }
  });
  const add = document.getElementById('add-line');
  if (add) add.addEventListener('click', () => {
    const total = document.getElementById('id_lines-TOTAL_FORMS');
    const count = Number(total.value);
    if (count >= 100) return;
    const template = document.getElementById('empty-line');
    const wrapper = document.createElement('div');
    // Template contains server-rendered form markup; no user values are inserted as HTML.
    wrapper.innerHTML = template.innerHTML.replaceAll('__prefix__', String(count));
    const fieldset = wrapper.firstElementChild;
    fieldset.querySelector('legend').textContent = `Baris ${count + 1}`;
    document.getElementById('line-forms').appendChild(fieldset);
    total.value = String(count + 1);
    fieldset.querySelector('select,input')?.focus();
  });
  document.querySelectorAll('[data-submit-form]').forEach(form => {
    form.addEventListener('submit', () => {
      form.querySelectorAll('button[type="submit"]').forEach(button => {
        button.disabled = true;
        button.textContent = 'Menyimpan…';
      });
    });
  });
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('[data-submit-form] button[type="submit"]').forEach(button => {
      if (button.disabled) { button.disabled = false; button.textContent = 'Simpan'; }
    });
  });
});
