document.documentElement.classList.add('js');

document.addEventListener('DOMContentLoaded', () => {
  const format = value => Number(value).toLocaleString('id-ID', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const splitYards = text => {
    let firstContent = true;
    return text.replaceAll(';', '\n').replaceAll('\t', '\n').split(/\r?\n/).flatMap(line => {
      if (!line.trim()) return [];
      const header = firstContent && /yard|roll|jumlah/i.test(line) && !/\d/.test(line);
      firstContent = false;
      return header ? [] : line.trim().split(/\s+/);
    });
  };
  const yardNumber = raw => {
    let value = raw.trim();
    if (value.includes('.') && value.includes(',')) {
      value = value.lastIndexOf(',') > value.lastIndexOf('.') ?
        value.replaceAll('.', '').replace(',', '.') : value.replaceAll(',', '');
    } else if (value.includes(',')) value = value.replace(',', '.');
    else if (value.includes('.') && value.split('.').at(-1).length >= 3)
      value = value.replaceAll('.', '');
    if (!/^\d+(\.\d{1,2})?$/.test(value)) return null;
    const parsed = Number(value);
    return parsed > 0 && parsed <= 99999999.99 ? parsed : null;
  };
  const flash = element => {
    element.classList.add('just-added');
    element.addEventListener('animationend', () => element.classList.remove('just-added'), {once: true});
  };
  const menu = document.getElementById('menu-toggle');
  const sidebar = document.getElementById('sidebar');
  const workspace = document.querySelector('.workspace');
  const mobileNav = window.matchMedia('(max-width: 900px)');
  const syncMenu = () => {
    const open = document.body.classList.contains('nav-open');
    if (sidebar) sidebar.inert = mobileNav.matches && !open;
    if (workspace) workspace.inert = mobileNav.matches && open;
  };
  const closeMenu = () => {
    document.body.classList.remove('nav-open');
    menu?.setAttribute('aria-expanded', 'false');
    menu?.setAttribute('aria-label', 'Buka menu');
    syncMenu();
  };
  menu?.addEventListener('click', () => {
    const open = document.body.classList.toggle('nav-open');
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? 'Tutup menu' : 'Buka menu');
    syncMenu();
    if (open) document.getElementById('sidebar-close')?.focus();
  });
  document.getElementById('sidebar-close')?.addEventListener('click', () => {
    closeMenu(); menu?.focus();
  });
  document.getElementById('nav-backdrop')?.addEventListener('click', () => {
    closeMenu(); menu?.focus();
  });
  sidebar?.querySelectorAll('nav a').forEach(link => link.addEventListener('click', closeMenu));
  mobileNav.addEventListener('change', closeMenu);
  syncMenu();
  document.addEventListener('keydown', event => {
    if (!document.body.classList.contains('nav-open')) return;
    if (event.key === 'Escape') { closeMenu(); menu?.focus(); }
    if (event.key === 'Tab' && mobileNav.matches) {
      const controls = [...sidebar.querySelectorAll('a,button')].filter(item => !item.disabled);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });

  document.querySelectorAll('[data-close-message]').forEach(button => {
    button.addEventListener('click', () => button.closest('.message')?.remove());
  });
  document.querySelectorAll('[data-confirm]').forEach(form => {
    form.addEventListener('submit', event => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  document.querySelectorAll('[data-open-dialog]').forEach(button => {
    button.addEventListener('click', () => {
      const dialog = document.getElementById(button.dataset.openDialog);
      if (!dialog) return;
      dialog.querySelectorAll('[data-dialog-field]').forEach(input => input.disabled = false);
      dialog.showModal();
    });
  });
  document.querySelectorAll('[data-close-dialog]').forEach(button =>
    button.addEventListener('click', () => button.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach(dialog => {
    dialog.addEventListener('close', () => {
      dialog.querySelectorAll('[data-dialog-field]').forEach(input => input.disabled = true);
    });
  });
  const sectionTabs = document.querySelector('[data-section-tabs]');
  if (sectionTabs) {
    const links = [...sectionTabs.querySelectorAll('a')];
    const syncTab = hash => links.forEach(link => {
      const activeHash = hash?.startsWith('#pengiriman-') ? '#pengiriman' : (hash || '#pengiriman');
      if (link.hash === activeHash) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    links.forEach(link => link.addEventListener('click', () => syncTab(link.hash)));
    window.addEventListener('hashchange', () => syncTab(window.location.hash));
    syncTab(window.location.hash.startsWith('#pengiriman-') ? '#pengiriman' : window.location.hash);
  }

  const allocationForm = document.getElementById('allocation-form');
  if (allocationForm) {
    const selection = [...allocationForm.querySelectorAll('input[name=roll][type=checkbox]')];
    const selectAll = document.getElementById('select-all');
    let lastSelected = null;
    const updateSelection = () => {
      const checked = selection.filter(box => box.checked);
      const total = checked.reduce((sum, box) => sum + (yardNumber(box.dataset.yard || '') || 0), 0);
      const text = `${checked.length} roll dipilih · ${format(total)} yard`;
      document.getElementById('selection-bar').hidden = !checked.length;
      document.getElementById('selection-summary').textContent = text;
      document.getElementById('dialog-summary').textContent = text;
      selection.forEach(box => box.closest('tr')?.classList.toggle('selected-row', box.checked));
      if (selectAll) {
        selectAll.checked = selection.length > 0 && checked.length === selection.length;
        selectAll.indeterminate = checked.length > 0 && checked.length < selection.length;
        selectAll.disabled = selection.length === 0;
      }
      allocationForm.querySelectorAll('[data-select-group]').forEach(group => {
        const rolls = selection.filter(box => box.dataset.group === group.dataset.selectGroup);
        group.disabled = rolls.length === 0;
        group.checked = rolls.length > 0 && rolls.every(box => box.checked);
        group.indeterminate = rolls.some(box => box.checked) && !group.checked;
      });
    };
    selection.forEach((box, index) => box.addEventListener('click', event => {
      if (event.shiftKey && lastSelected !== null) {
        for (let at = Math.min(index, lastSelected); at <= Math.max(index, lastSelected); at++)
          selection[at].checked = box.checked;
      }
      lastSelected = index; updateSelection();
    }));
    selectAll?.addEventListener('change', () => {
      selection.forEach(box => box.checked = selectAll.checked); updateSelection();
    });
    allocationForm.querySelectorAll('[data-select-group]').forEach(group =>
      group.addEventListener('change', () => {
        selection.filter(box => box.dataset.group === group.dataset.selectGroup)
          .forEach(box => box.checked = group.checked);
        updateSelection();
      }));
    if (document.getElementById('selection-bar')) updateSelection();
  }
  document.querySelectorAll('[data-receive-form]').forEach(form => {
    const boxes = [...form.querySelectorAll('input[name=rolls]')];
    const all = form.querySelector('[data-select-receive-all]');
    const update = () => {
      if (boxes.some(box => box.checked)) boxes[0]?.setCustomValidity('');
      boxes.forEach(box => box.closest('tr').classList.toggle('selected-row', box.checked));
      if (all) {
        all.disabled = !boxes.length;
        all.checked = boxes.length > 0 && boxes.every(box => box.checked);
        all.indeterminate = boxes.some(box => box.checked) && !all.checked;
      }
    };
    boxes.forEach(box => box.addEventListener('change', update));
    all?.addEventListener('change', () => { boxes.forEach(box => box.checked = all.checked); update(); });
    form.addEventListener('submit', event => {
      if (boxes.length && !boxes.some(box => box.checked)) {
        event.preventDefault(); boxes[0].setCustomValidity('Pilih minimal satu roll yang sudah diterima.');
        boxes[0].reportValidity();
      }
    });
    boxes.forEach(box => box.addEventListener('change', () => boxes[0]?.setCustomValidity('')));
    update();
  });

  const invoiceForm = document.getElementById('invoice-form');
  if (invoiceForm) {
    const groupContainer = document.getElementById('groups');
    const existingCount = Number(groupContainer.dataset.existingCount || 0);
    const extraLimit = Math.max(0, 10 - existingCount);
    const rowState = new WeakMap();
    let importing = 0;
    const updateInvoiceSummary = () => {
      const groups = [...groupContainer.querySelectorAll('[data-group]')];
      const rows = groups.flatMap(group => [...group.querySelectorAll('[data-color-row]')]);
      const values = rows.flatMap(row => splitYards(row.querySelector('[data-yard-source]').value)
        .map(yardNumber).filter(value => value !== null));
      const existingRows = [...invoiceForm.querySelectorAll('.roll-edit-table tbody tr')];
      const recorded = existingRows.map(row => yardNumber(row.querySelector('input[name^="existing-"]:not([name$="-material"]):not([name$="-color"]):not([name$="-lokasi"])')?.value || '')).filter(value => value !== null);
      const recordedColors = new Set(existingRows.map(row => row.dataset.existingColor ||
        `${row.querySelector('[name$="-material"]').value}|${row.querySelector('[name$="-color"]').value}|${row.querySelector('[name$="-lokasi"]').value}`));
      const allValues = [...recorded, ...values];
      document.getElementById('invoice-summary').textContent =
        `${existingCount + groups.length} bahan · ${recordedColors.size + rows.length} baris warna · ${allValues.length} roll · ${format(allValues.reduce((sum, value) => sum + value, 0))} yard`;
    };
    const renumber = () => {
      const groups = [...groupContainer.querySelectorAll('[data-group]')];
      document.getElementById('group-count').value = groups.length;
      const add = document.getElementById('add-group');
      add.disabled = groups.length >= extraLimit;
      document.getElementById('group-limit').textContent = existingCount >= 10 ?
        'Batas 10 bahan sudah tercatat. Anda tetap dapat memperbarui invoice dan roll tersedia.' :
        groups.length >= extraLimit ? 'Batas 10 bahan tercapai.' :
        `Maksimal 10 bahan per invoice (${existingCount + groups.length} panel bahan).`;
      groups.forEach((group, gi) => {
        group.querySelector('[data-group-number]').textContent = gi + 1;
        const material = group.querySelector('[data-material-input]');
        material.name = `groups-${gi}-material`; material.id = `group-${gi}-material`;
        material.previousElementSibling.setAttribute('for', material.id);
        group.querySelector('[data-row-count]').name = `groups-${gi}-row_count`;
        const rows = [...group.querySelectorAll('[data-color-row]')];
        group.querySelector('[data-row-count]').value = rows.length;
        rows.forEach((row, ri) => {
          row.querySelector('[data-row-number]').textContent = ri + 1;
          [['color','[data-color-input]'],['lokasi','[data-location-input]'],['yards','[data-yard-source]']]
            .forEach(([key, selector]) => {
              const input = row.querySelector(selector);
              input.name = `groups-${gi}-rows-${ri}-${key}`;
              input.id = `group-${gi}-row-${ri}-${key}`;
              row.querySelector(key === 'yards' ? '.yard-label' : `label[for$="-${key}"]`)
                ?.setAttribute('for', input.id);
            });
          row.querySelector('[data-file]').name = `file-${gi}-${ri}`;
          row.querySelector('[data-remove-color]').disabled = rows.length === 1;
        });
        group.querySelector('[data-remove-group]').disabled = groups.length === 1 && !existingCount;
      });
      updateInvoiceSummary();
    };
    const setupRow = row => {
      const source = row.querySelector('[data-yard-source]');
      const grid = row.querySelector('[data-yard-grid]');
      const file = row.querySelector('[data-file]');
      const inputs = () => [...grid.querySelectorAll('input')];
      const addYard = (value = '') => {
        const wrapper = document.createElement('div'); wrapper.className = 'yard-row';
        const index = document.createElement('span'); index.textContent = grid.children.length + 1;
        const input = document.createElement('input'); input.type = 'text'; input.inputMode = 'decimal'; input.value = value;
        input.setAttribute('aria-label', `Yard roll ${index.textContent}`);
        const preview = document.createElement('span'); preview.className = 'check';
        wrapper.append(index, input, preview); grid.append(wrapper); return input;
      };
      const update = () => {
        const raw = inputs().filter(input => input.value.trim()).map(input => input.value.trim());
        source.value = raw.join('\n');
        const values = raw.map(yardNumber).filter(value => value !== null);
        const total = values.reduce((sum, value) => sum + value, 0);
        const sorted = [...values].sort((a, b) => a - b), mid = Math.floor(sorted.length / 2);
        const median = sorted.length % 2 ? sorted[mid] : ((sorted[mid - 1] || 0) + (sorted[mid] || 0)) / 2;
        let ambiguous = 0;
        inputs().forEach((input, at) => {
          const value = input.value.trim() ? yardNumber(input.value) : null;
          const invalid = input.value.trim() && value === null;
          input.setCustomValidity(invalid ? 'Yard harus angka positif dengan maksimal dua angka desimal.' : '');
          input.classList.toggle('invalid', Boolean(invalid));
          input.setAttribute('aria-invalid', String(Boolean(invalid)));
          input.setAttribute('aria-label', `Yard roll ${at + 1}`);
          input.parentElement.firstElementChild.textContent = at + 1;
          const isAmbiguous = /^\d+\.\d{3,}$/.test(input.value.trim());
          if (isAmbiguous) ambiguous++;
          const unusual = value && median && (value > median * 3 || value < median * .2);
          input.parentElement.querySelector('.check').textContent = isAmbiguous ? 'cek format' : unusual ? 'cek yard' : '';
          input.title = value === null ? '' : `Dibaca sebagai ${format(value)} yard`;
        });
        const error = row.querySelector('[data-yard-error]');
        error.textContent = raw.length > values.length ? `${raw.length - values.length} nilai yard perlu diperbaiki.` :
          ambiguous ? 'Titik dengan 3 angka berikutnya dibaca sebagai pemisah ribuan. Periksa total yard sebelum menyimpan.' : '';
        row.querySelector('[data-roll-count]').textContent = values.length;
        row.querySelector('[data-yard-total]').textContent = format(total);
        row.querySelector('[data-yard-average]').textContent = values.length ? format(total / values.length) : '—';
        row.querySelector('[data-yard-min]').textContent = values.length ? format(Math.min(...values)) : '—';
        row.querySelector('[data-yard-max]').textContent = values.length ? format(Math.max(...values)) : '—';
        updateInvoiceSummary();
      };
      const replace = values => {
        grid.replaceChildren(); values.forEach(value => addYard(value)); addYard(); update();
      };
      rowState.set(row, {replace, update, inputs});
      splitYards(source.value).forEach(value => addYard(value)); addYard();
      grid.addEventListener('input', update);
      grid.addEventListener('keydown', event => {
        if (!event.target.matches('input')) return;
        const all = inputs(), at = all.indexOf(event.target);
        if (event.key === 'Enter' || event.key === 'ArrowDown') {
          event.preventDefault(); (all[at + 1] || addYard()).focus();
        } else if (event.key === 'ArrowUp' && at > 0) {
          event.preventDefault(); all[at - 1].focus();
        } else if (event.key === 'Backspace' && !event.target.value && all.length > 1) {
          event.preventDefault(); event.target.parentElement.remove();
          inputs()[Math.max(0, at - 1)]?.focus(); update();
        }
      });
      grid.addEventListener('paste', event => {
        if (!event.target.matches('input')) return;
        const parts = splitYards(event.clipboardData.getData('text'));
        if (parts.length < 2) return;
        event.preventDefault(); const at = inputs().indexOf(event.target);
        parts.forEach((value, offset) => { (inputs()[at + offset] || addYard()).value = value; });
        update(); (inputs()[at + parts.length] || addYard()).focus();
      });
      const paste = row.querySelector('.paste-box'), pasteText = row.querySelector('[data-paste-text]');
      row.querySelector('[data-paste-open]').addEventListener('click', event => {
        paste.hidden = !paste.hidden; event.currentTarget.setAttribute('aria-expanded', String(!paste.hidden));
        if (!paste.hidden) pasteText.focus();
      });
      pasteText.addEventListener('input', () => {
        const parts = splitYards(pasteText.value), values = parts.map(yardNumber).filter(value => value !== null);
        row.querySelector('[data-paste-preview]').textContent = `${values.length} roll · ${format(values.reduce((sum, value) => sum + value, 0))} yard${parts.length > values.length ? ` · ${parts.length - values.length} nilai belum valid` : ''}`;
      });
      row.querySelector('[data-apply-paste]').addEventListener('click', () => {
        replace(splitYards(pasteText.value)); paste.hidden = true;
        row.querySelector('[data-paste-open]').setAttribute('aria-expanded', 'false'); inputs()[0]?.focus();
      });
      row.querySelector('[data-clear-yards]').addEventListener('click', () => {
        replace([]); file.value = ''; row.querySelector('[data-import-status]').textContent = ''; inputs()[0]?.focus();
      });
      file.addEventListener('change', async () => {
        const upload = file.files?.[0]; if (!upload) return;
        const status = row.querySelector('[data-import-status]');
        status.classList.remove('field-error');
        status.textContent = `Membaca ${upload.name}…`; importing++; file.disabled = true;
        try {
          const body = new FormData(); body.append('file', upload);
          const response = await fetch(invoiceForm.dataset.importPreviewUrl, {
            method: 'POST', body, credentials: 'same-origin',
            headers: {'X-CSRFToken': invoiceForm.querySelector('[name=csrfmiddlewaretoken]').value},
          });
          const result = await response.json();
          if (!response.ok || result.errors?.length) throw new Error(result.errors?.join(' ') || 'File yard tidak dapat dibaca.');
          const current = splitYards(source.value); replace([...current, ...result.yards]);
          status.textContent = `${upload.name}: ${result.roll_count} roll · ${format(result.total_yard)} yard ditambahkan ke warna ini. Periksa daftar sebelum menyimpan.`;
          file.value = '';
        } catch (error) {
          status.textContent = error.message || 'Impor belum berhasil. Periksa file lalu pilih kembali.';
          status.classList.add('field-error'); file.value = '';
        } finally { importing--; file.disabled = false; }
      });
      row.querySelector('[data-remove-color]').addEventListener('click', () => {
        if (row.parentElement.children.length === 1) return;
        if (source.value && !window.confirm('Hapus baris warna dan daftar yard yang belum disimpan ini?')) return;
        const group = row.closest('[data-group]'); row.remove(); renumber(); group.querySelector('[data-add-color]').focus();
      });
      update();
    };
    const setupGroup = group => {
      const material = group.querySelector('[data-material-input]');
      const title = group.querySelector('[data-material-title]');
      const updateTitle = () => title.textContent = material.value.trim() ? ` · ${material.value.trim()}` : '';
      material.addEventListener('input', updateTitle); updateTitle();
      group.querySelectorAll('[data-color-row]').forEach(setupRow);
      group.querySelector('[data-add-color]').addEventListener('click', () => {
        const row = document.getElementById('color-row-template').content.firstElementChild.cloneNode(true);
        group.querySelector('[data-color-rows]').append(row); setupRow(row); renumber(); flash(row); row.querySelector('[data-color-input]').focus();
      });
      group.querySelector('[data-remove-group]').addEventListener('click', () => {
        if (groupContainer.children.length === 1 && !existingCount) return;
        const hasValue = [...group.querySelectorAll('[data-yard-source]')].some(input => input.value.trim());
        if (hasValue && !window.confirm('Hapus bahan beserta warna dan roll yang belum disimpan ini?')) return;
        group.remove(); renumber(); document.getElementById('add-group').focus();
      });
    };
    groupContainer.querySelectorAll('[data-group]').forEach(setupGroup); renumber();
    document.getElementById('add-group').addEventListener('click', () => {
      if (groupContainer.children.length >= extraLimit) return;
      const group = document.getElementById('group-template').content.firstElementChild.cloneNode(true);
      groupContainer.append(group); setupGroup(group); renumber(); flash(group); group.querySelector('[data-material-input]').focus();
    });
    invoiceForm.addEventListener('submit', event => {
      renumber();
      if (importing) { event.preventDefault(); invoiceForm.querySelector('[data-import-status]').textContent = 'Tunggu sampai impor selesai sebelum menyimpan.'; return; }
      const editing = groupContainer.dataset.edit === 'true';
      let invalid = null;
      for (const group of groupContainer.querySelectorAll('[data-group]')) {
        const rows = [...group.querySelectorAll('[data-color-row]')];
        const material = group.querySelector('[data-material-input]');
        const active = !editing || material.value.trim() || rows.some(row => row.querySelector('[data-yard-source]').value.trim() || row.querySelector('[data-color-input]').value.trim() || row.querySelector('[data-location-input]').value.trim());
        if (!active) continue;
        if (!material.value.trim()) { material.setCustomValidity('Isi nama bahan.'); invalid = material; break; }
        material.setCustomValidity('');
        for (const row of rows) {
          const color = row.querySelector('[data-color-input]'), state = rowState.get(row);
          if (!color.value.trim()) { color.setCustomValidity('Isi warna bahan.'); invalid = color; break; }
          color.setCustomValidity('');
          const yardInputs = state.inputs(), nonempty = yardInputs.filter(input => input.value.trim());
          if (!nonempty.length && !row.querySelector('[data-file]').files.length) {
            row.querySelector('[data-yard-error]').textContent = 'Isi minimal satu roll dengan yard positif.';
            invalid = yardInputs[0]; invalid.setCustomValidity('Isi minimal satu roll dengan yard positif.'); break;
          }
          invalid = yardInputs.find(input => !input.checkValidity()); if (invalid) break;
        }
        if (invalid) break;
      }
      if (invalid) { event.preventDefault(); invalid.focus(); invalid.reportValidity(); }
    });
    invoiceForm.addEventListener('input', event => {
      if (!event.target.closest('[data-yard-grid]')) event.target.setCustomValidity?.('');
      if (event.target.closest('.roll-edit-table')) updateInvoiceSummary();
    });
  }

  const shipColor = document.getElementById('ship-color');
  const shipPcs = document.getElementById('ship-pcs');
  const shipmentContext = document.getElementById('ship-context');
  const updateShipment = () => {
    const option = shipColor?.selectedOptions[0];
    const submit = shipColor?.closest('form').querySelector('button[type=submit]');
    if (!option?.value) {
      shipPcs?.removeAttribute('max');
      if (shipPcs) shipPcs.disabled = false;
      if (submit) submit.disabled = false;
      if (shipmentContext) shipmentContext.textContent = 'Pilih warna untuk melihat hasil, total terkirim, dan sisa sebelum mencatat kiriman.';
      return;
    }
    if (option.dataset.hasil === '') {
      shipmentContext.textContent = 'Hasil warna ini belum diisi. Catat total hasil produksi terlebih dahulu.';
      shipPcs.disabled = true;
      if (submit) submit.disabled = true;
    } else {
      const remaining = Number(option.dataset.remaining);
      shipmentContext.textContent = `Hasil ${option.dataset.hasil} pcs · sudah terkirim ${option.dataset.shipped} pcs · sisa kirim ${remaining} pcs.`;
      shipPcs.disabled = remaining === 0; shipPcs.max = remaining;
      if (submit) submit.disabled = remaining === 0;
      if (remaining === 0) shipmentContext.textContent += ' Seluruh hasil tercatat sudah dikirim.';
    }
  };
  shipColor?.addEventListener('change', updateShipment); if (shipColor) updateShipment();

  document.querySelectorAll('[data-submit-form]').forEach(form => {
    form.addEventListener('submit', event => {
      if (event.defaultPrevented) return;
      if (form.dataset.submitted) { event.preventDefault(); return; }
      form.dataset.submitted = 'true'; form.setAttribute('aria-busy', 'true');
      const button = event.submitter;
      if (button?.name) {
        const intent = document.createElement('input'); intent.type = 'hidden'; intent.name = button.name; intent.value = button.value;
        intent.dataset.submitIntent = 'true'; form.append(intent);
      }
      form.querySelectorAll('button[type=submit],button:not([type])').forEach(item => {
        item.dataset.wasDisabled = String(item.disabled);
        item.dataset.originalLabel = item.textContent;
        item.disabled = true; if (item === button) item.textContent = 'Menyimpan…';
      });
    });
  });
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('[data-submit-form]').forEach(form => {
      delete form.dataset.submitted; form.removeAttribute('aria-busy');
      form.querySelectorAll('[data-submit-intent]').forEach(input => input.remove());
      form.querySelectorAll('[data-original-label]').forEach(button => {
        button.textContent = button.dataset.originalLabel;
        button.disabled = button.dataset.wasDisabled === 'true';
        delete button.dataset.originalLabel; delete button.dataset.wasDisabled;
      });
    });
  });
  const errorField = document.querySelector('.errorlist')?.closest('.field')?.querySelector('input,select,textarea');
  if (errorField) { errorField.setAttribute('aria-invalid', 'true'); errorField.focus(); }
  else document.querySelector('[data-error-summary]')?.focus();
});
