document.documentElement.classList.add('js');

document.addEventListener('DOMContentLoaded', () => {
  const menu = document.getElementById('menu-toggle');
  menu?.addEventListener('click', () => {
    const open = document.body.classList.toggle('nav-open');
    menu.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      document.body.classList.remove('nav-open');
      menu?.setAttribute('aria-expanded', 'false');
    }
  });
  document.querySelectorAll('[data-close-message]').forEach(button => {
    button.addEventListener('click', () => button.closest('.message').remove());
    setTimeout(() => button.closest('.message')?.remove(), 4000);
  });
  document.querySelectorAll('[data-confirm]').forEach(form => {
    form.addEventListener('submit', event => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  document.querySelectorAll('[data-submit-form]').forEach(form => {
    form.addEventListener('submit', event => {
      if (form.dataset.submitted) {
        event.preventDefault();
        return;
      }
      form.dataset.submitted = 'true';
      const button = event.submitter;
      if (button?.name) {
        const intent = document.createElement('input');
        intent.type = 'hidden';
        intent.name = button.name;
        intent.value = button.value;
        form.append(intent);
      }
      form.querySelectorAll('button[type=submit],button:not([type])').forEach(item => {
        item.disabled = true;
        if (item === button) item.textContent = 'Menyimpan…';
      });
    });
  });
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('[data-submit-form]').forEach(form => {
      delete form.dataset.submitted;
      form.querySelectorAll('button').forEach(button => button.disabled = false);
    });
  });
  document.querySelectorAll('[data-open-dialog]').forEach(button => {
    button.addEventListener('click', () => {
      document.getElementById(button.dataset.openDialog)?.showModal();
    });
  });
  document.querySelectorAll('[data-close-dialog]').forEach(button => {
    button.addEventListener('click', () => button.closest('dialog').close());
  });

  const selection = document.querySelectorAll('input[name=roll][type=checkbox]');
  const selectAll = document.getElementById('select-all');
  let lastSelected = null;
  const updateSelection = () => {
    const checked = [...selection].filter(box => box.checked);
    const sum = checked.reduce((total, box) => total + Number(box.dataset.yard || 0), 0);
    const label = `${checked.length} roll · ${format(sum)} yd`;
    const bar = document.getElementById('selection-bar');
    if (bar) bar.hidden = !checked.length;
    const summary = document.getElementById('selection-summary');
    if (summary) summary.textContent = label;
    const dialogSummary = document.getElementById('dialog-summary');
    if (dialogSummary) dialogSummary.textContent = label;
  };
  selection.forEach((box, index) => box.addEventListener('click', event => {
    if (event.shiftKey && lastSelected !== null) {
      const start = Math.min(lastSelected, index);
      const end = Math.max(lastSelected, index);
      for (let i = start; i <= end; i++) selection[i].checked = box.checked;
    }
    lastSelected = index;
    updateSelection();
  }));
  selectAll?.addEventListener('change', () => {
    selection.forEach(box => box.checked = selectAll.checked);
    updateSelection();
  });
  document.querySelectorAll('[data-select-group]').forEach(box => {
    box.addEventListener('change', () => {
      selection.forEach(roll => {
        if (roll.dataset.group === box.dataset.selectGroup) roll.checked = box.checked;
      });
      updateSelection();
    });
  });
  const poInput = document.getElementById('allocation-po');
  poInput?.addEventListener('input', () => {
    const known = [...document.querySelectorAll('#po-list [data-value]')]
      .some(option => option.dataset.value.toUpperCase() === poInput.value.toUpperCase());
    document.getElementById('new-po-hint').hidden = !poInput.value.trim() || known;
  });

  const format = value => Number(value).toLocaleString('id-ID', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const number = raw => {
    let text = raw.trim();
    if (text.includes('.') && text.includes(',')) {
      const commaLast = text.lastIndexOf(',') > text.lastIndexOf('.');
      text = commaLast ? text.replaceAll('.', '').replace(',', '.')
        : text.replaceAll(',', '');
    } else if (text.includes(',')) text = text.replace(',', '.');
    else if (text.includes('.') && text.split('.').at(-1).length >= 3)
      text = text.replaceAll('.', '');
    if (!/^\d+(\.\d{1,2})?$/.test(text)) return null;
    const value = Number(text);
    return value > 0 ? value : null;
  };
  const splitYards = text => text.split(/[\s;]+/).filter(Boolean);
  const setupCombo = (combo, index) => {
    const input = combo.querySelector('input');
    const list = combo.querySelector('[role=listbox]');
    if (!input || !list) return;
    const id = `combo-${index}`;
    input.id = id;
    list.id = `${id}-options`;
    input.setAttribute('aria-controls', list.id);
    combo.parentElement.querySelector('label')?.setAttribute('for', id);
    let active = -1;
    const refresh = () => {
      const query = input.value.trim().toLocaleLowerCase('id');
      const options = [...list.querySelectorAll('[data-value]')];
      let found = false;
      options.forEach(option => {
        const match = option.dataset.value.toLocaleLowerCase('id').includes(query);
        option.hidden = !match;
        if (match) found = true;
        const label = option.dataset.value;
        option.replaceChildren();
        const at = label.toLocaleLowerCase('id').indexOf(query);
        if (at >= 0 && query) {
          option.append(document.createTextNode(label.slice(0, at)));
          const mark = document.createElement('strong');
          mark.textContent = label.slice(at, at + query.length);
          option.append(mark, document.createTextNode(label.slice(at + query.length)));
        } else option.textContent = label;
      });
      let create = list.querySelector('[data-create]');
      if (!create) {
        create = document.createElement('button');
        create.type = 'button';
        create.setAttribute('role', 'option');
        create.dataset.create = 'true';
        list.append(create);
      }
      create.textContent = `Buat "${input.value.trim()}" sebagai baru`;
      create.hidden = !query || found;
      list.hidden = !query && !document.activeElement.isSameNode(input);
      input.setAttribute('aria-expanded', String(!list.hidden));
      active = -1;
    };
    input.addEventListener('focus', refresh);
    input.addEventListener('input', refresh);
    input.addEventListener('keydown', event => {
      const options = [...list.querySelectorAll('button:not([hidden])')];
      if (event.key === 'Escape') {
        list.hidden = true;
        input.setAttribute('aria-expanded', 'false');
      }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        active = (active + (event.key === 'ArrowDown' ? 1 : -1) + options.length)
          % options.length;
        options.forEach((option, idx) => option.setAttribute(
          'aria-selected', String(idx === active)
        ));
      }
      if (event.key === 'Enter' && !list.hidden && active >= 0) {
        event.preventDefault();
        options[active].click();
      } else if (event.key === 'Enter' && input.closest('#invoice-form')) {
        event.preventDefault();
        const next = combo.closest('.field')?.nextElementSibling
          ?.querySelector('input,select,textarea');
        (next || combo.closest('[data-group]')?.querySelector('[data-yard-grid] input'))
          ?.focus();
      }
    });
    list.addEventListener('click', event => {
      const option = event.target.closest('button');
      if (!option) return;
      if (option.dataset.value) input.value = option.dataset.value;
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.focus();
    });
    document.addEventListener('click', event => {
      if (!combo.contains(event.target)) list.hidden = true;
    });
  };
  let comboIndex = 0;
  document.querySelectorAll('[data-combo]').forEach(combo => setupCombo(combo, comboIndex++));

  const setupGroup = group => {
    const grid = group.querySelector('[data-yard-grid]');
    const source = group.querySelector('[data-yard-source]');
    group.querySelector('[data-note-open]').addEventListener('click', () => {
      const note = group.querySelector('.group-note>div');
      note.hidden = !note.hidden;
      if (!note.hidden) note.querySelector('textarea').focus();
    });
    const values = splitYards(source.value);
    const addRow = (value = '') => {
      const row = document.createElement('div');
      row.className = 'yard-row';
      const count = document.createElement('span');
      count.textContent = String(grid.children.length + 1);
      const input = document.createElement('input');
      input.type = 'text';
      input.inputMode = 'decimal';
      input.value = value;
      input.setAttribute('aria-label', `Yard roll ${count.textContent}`);
      const check = document.createElement('span');
      check.className = 'check';
      row.append(count, input, check);
      grid.append(row);
      return input;
    };
    const inputs = () => [...grid.querySelectorAll('input')];
    const update = () => {
      const raw = inputs().map(input => input.value.trim()).filter(Boolean);
      source.value = raw.join('\n');
      const valid = raw.map(number).filter(value => value !== null);
      const invalid = raw.length - valid.length;
      const total = valid.reduce((sum, value) => sum + value, 0);
      const ordered = [...valid].sort((a, b) => a - b);
      const middle = Math.floor(ordered.length / 2);
      const median = ordered.length % 2 ? ordered[middle] :
        ((ordered[middle - 1] || 0) + (ordered[middle] || 0)) / 2;
      inputs().forEach(input => {
        const value = input.value.trim() ? number(input.value) : null;
        input.classList.toggle('invalid', Boolean(input.value.trim() && value === null));
        input.parentElement.querySelector('.check').textContent = value && median &&
          (value > 3 * median || value < .2 * median) ? 'cek' : '';
      });
      group.querySelector('[data-yard-error]').textContent = invalid ?
        `${invalid} yard perlu diperbaiki.` : '';
      group.querySelector('[data-roll-count]').textContent = String(valid.length);
      group.querySelector('[data-yard-total]').textContent = format(total);
      group.querySelector('[data-yard-average]').textContent = valid.length ?
        format(total / valid.length) : '—';
      group.querySelector('[data-yard-min]').textContent = valid.length ?
        format(Math.min(...valid)) : '—';
      group.querySelector('[data-yard-max]').textContent = valid.length ?
        format(Math.max(...valid)) : '—';
      updateInvoiceSummary();
    };
    grid.addEventListener('input', update);
    grid.addEventListener('keydown', event => {
      if (event.target.tagName !== 'INPUT') return;
      const all = inputs();
      const at = all.indexOf(event.target);
      if (event.key === 'Enter') {
        event.preventDefault();
        (all[at + 1] || addRow()).focus();
      } else if (event.key === 'ArrowDown') {
        event.preventDefault();
        (all[at + 1] || addRow()).focus();
      } else if (event.key === 'ArrowUp' && at > 0) {
        event.preventDefault();
        all[at - 1].focus();
      } else if (event.key === 'Backspace' && !event.target.value && all.length > 1) {
        event.preventDefault();
        event.target.parentElement.remove();
        all[Math.max(0, at - 1)].focus();
        inputs().forEach((input, index) => {
          input.parentElement.firstChild.textContent = String(index + 1);
        });
        update();
      }
    });
    grid.addEventListener('paste', event => {
      const text = event.clipboardData.getData('text');
      const parts = splitYards(text);
      if (parts.length < 2) return;
      event.preventDefault();
      const at = inputs().indexOf(event.target);
      parts.forEach((value, offset) => {
        const input = inputs()[at + offset] || addRow();
        input.value = value;
      });
      update();
      (inputs()[at + parts.length] || addRow()).focus();
    });
    group.querySelector('[data-paste-open]').addEventListener('click', () => {
      const box = group.querySelector('.paste-box');
      box.hidden = !box.hidden;
      if (!box.hidden) box.querySelector('textarea').focus();
    });
    const pasteText = group.querySelector('[data-paste-text]');
    pasteText.addEventListener('input', () => {
      const parsed = splitYards(pasteText.value).map(number).filter(value => value !== null);
      group.querySelector('[data-paste-preview]').textContent =
        `${parsed.length} roll · ${format(parsed.reduce((a, b) => a + b, 0))} yd`;
    });
    group.querySelector('[data-apply-paste]').addEventListener('click', () => {
      const parts = splitYards(pasteText.value);
      grid.replaceChildren();
      parts.forEach(value => addRow(value));
      addRow().focus();
      group.querySelector('.paste-box').hidden = true;
      update();
    });
    group.querySelector('[data-clear-yards]').addEventListener('click', () => {
      grid.replaceChildren();
      addRow().focus();
      update();
    });
    group.querySelector('[data-remove-group]').addEventListener('click', () => {
      if (document.querySelectorAll('[data-group]').length === 1) return;
      if (source.value && !window.confirm('Hapus bahan dan daftar yard ini?')) return;
      group.remove();
      renumberGroups();
      updateInvoiceSummary();
    });
    values.forEach(value => addRow(value));
    addRow();
    update();
  };
  const updateInvoiceSummary = () => {
    const label = document.getElementById('invoice-summary');
    if (!label) return;
    const groups = [...document.querySelectorAll('[data-group]')];
    const values = groups.flatMap(group => splitYards(
      group.querySelector('[data-yard-source]').value
    ).map(number).filter(value => value !== null));
    label.textContent = `${groups.length} grup · ${values.length} roll · ${format(
      values.reduce((sum, value) => sum + value, 0)
    )} yd`;
  };
  const renumberGroups = () => {
    document.querySelectorAll('[data-group]').forEach((group, index) => {
      group.querySelector('[data-group-number]').textContent = String(index + 1);
      group.querySelector('[data-file]').name = `file-${index}`;
    });
  };
  const invoiceForm = document.getElementById('invoice-form');
  if (invoiceForm?.dataset.restore === 'true') {
    try {
      const saved = JSON.parse(sessionStorage.getItem(
        `invoice-draft:${window.location.pathname}`
      ) || '[]');
      saved.forEach((data, index) => {
        let group = document.querySelectorAll('[data-group]')[index];
        if (!group) {
          group = document.getElementById('group-template').content.firstElementChild
            .cloneNode(true);
          document.getElementById('groups').append(group);
          group.querySelectorAll('[data-combo]').forEach(combo => setupCombo(
            combo, comboIndex++
          ));
        }
        ['material[]', 'color[]', 'lokasi[]'].forEach((name, at) => {
          group.querySelector(`[name="${name}"]`).value = data.fields[at];
        });
        group.querySelector('[name="catatan[]"]').value = data.note;
        group.querySelector('[data-yard-source]').value = data.yards;
      });
    } catch { sessionStorage.removeItem(`invoice-draft:${window.location.pathname}`); }
  }
  renumberGroups();
  document.querySelectorAll('[data-group]').forEach(setupGroup);
  invoiceForm?.addEventListener('submit', () => {
    const saved = [...document.querySelectorAll('[data-group]')].map(group => ({
      fields: ['material[]', 'color[]', 'lokasi[]'].map(name =>
        group.querySelector(`[name="${name}"]`).value
      ),
      note: group.querySelector('[name="catatan[]"]').value,
      yards: group.querySelector('[data-yard-source]').value,
    }));
    sessionStorage.setItem(`invoice-draft:${window.location.pathname}`,
      JSON.stringify(saved));
  });
  document.getElementById('add-group')?.addEventListener('click', () => {
    const group = document.getElementById('group-template').content.firstElementChild
      .cloneNode(true);
    document.getElementById('groups').append(group);
    renumberGroups();
    group.querySelectorAll('[data-combo]').forEach(combo => setupCombo(
      combo, comboIndex++
    ));
    setupGroup(group);
    group.querySelector('input[name="material[]"]').focus();
  });
});
