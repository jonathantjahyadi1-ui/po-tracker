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
    form.addEventListener('submit', (event) => {
      // Disabled buttons are omitted from POST, so keep the selected action in a hidden field.
      form.querySelectorAll('[data-submit-intent]').forEach(input => input.remove());
      const submitter = event.submitter;
      if (submitter?.name) {
        const intent = document.createElement('input');
        intent.type = 'hidden';
        intent.name = submitter.name;
        intent.value = submitter.value;
        intent.dataset.submitIntent = 'true';
        form.appendChild(intent);
      }
      form.querySelectorAll('button[type="submit"]').forEach(button => {
        button.dataset.originalLabel = button.innerHTML;
        button.disabled = true;
        button.textContent = 'Menyimpan…';
      });
    });
  });
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('[data-submit-form] button[type="submit"]').forEach(button => {
      if (button.disabled) {
        button.disabled = false;
        // Saved markup comes only from the server-rendered button.
        button.innerHTML = button.dataset.originalLabel || 'Simpan';
      }
    });
  });
});
