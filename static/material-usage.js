document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-usage-form]');
  if (!form) return;
  const rows = form.querySelector('[data-usage-rows]');
  const total = form.querySelector('[name="form-TOTAL_FORMS"]');
  form.querySelector('[data-add-usage]')?.addEventListener('click', () => {
    if (Number(total.value) >= 500) return;
    const template = document.getElementById('empty-usage-row');
    const host = document.createElement('div');
    host.innerHTML = template.innerHTML.replaceAll('__prefix__', total.value);
    const row = host.firstElementChild;
    rows.append(row); total.value = Number(total.value) + 1;
    row.querySelector('select').focus();
  });
  rows.addEventListener('click', event => {
    if (!event.target.closest('[data-remove-usage]')) return;
    const row = event.target.closest('.usage-row');
    // Keep management indices stable; an emptied extra form is ignored by Django.
    row.querySelectorAll('input:not([type=checkbox]),select').forEach(input => input.value = '');
    row.querySelector('[type=checkbox]').checked = true;
    row.hidden = true;
  });
});
