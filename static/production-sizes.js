document.addEventListener('DOMContentLoaded', () => {
  const source = document.getElementById('production-size-data');
  if (!source) return;
  let items = JSON.parse(source.textContent);
  let openKey = null;
  const dirty = new Set();
  const shippingDrafts = new Map();
  const toggles = [...document.querySelectorAll('[data-size-toggle]')];
  const notice = document.getElementById('size-notice');
  const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
  const shipForm = document.querySelector('[data-size-shipment]');
  const shipMaterial = document.getElementById('ship-material');
  const shipColor = document.getElementById('ship-color');
  let shippingKey = null;
  let counter = 0;
  const element = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const button = (text, cls = 'button') => {
    const node = element('button', text, cls); node.type = 'button'; return node;
  };
  const itemFor = key => items[key] || {id: null, sizes: [], pending: [], sizes_complete: false, shipments_complete: false};
  const panelFor = key => document.querySelector('[data-size-panel="' + key + '"]');
  const toggleFor = key => toggles.find(t => t.dataset.sizeToggle === key);
  const mainFor = key => document.querySelector('[data-production-row="' + key + '"]');
  const alertBox = () => {
    const node = element('div', '', 'field-error'); node.hidden = true; node.tabIndex = -1;
    node.setAttribute('role', 'alert'); return node;
  };
  const editorSnapshot = key => {
    const host = panelFor(key).querySelector('[data-editor-host]');
    return JSON.stringify({
      sizes: [...host.querySelectorAll('[data-size-row]')].map(row => ({
        id: row.dataset.id, label: row.querySelector('[data-size-label]').value,
        pcs: row.querySelector('[data-size-pcs]').value,
      })),
      reconciliation: [...host.querySelectorAll('[data-size-id]')].map(input => input.value),
    });
  };
  const markDirty = key => {
    const host = panelFor(key).querySelector('[data-editor-host]');
    const changed = editorSnapshot(key) !== host.dataset.baseline;
    if (changed) dirty.add(key); else dirty.delete(key);
    mainFor(key).querySelector('[data-size-dirty]').hidden = !changed;
  };
  const clearDirty = key => {
    dirty.delete(key); mainFor(key).querySelector('[data-size-dirty]').hidden = true;
  };
  const inputField = (label, value, {number = false, min = 0, max, readonly = false} = {}) => {
    const field = element('div', undefined, 'field');
    const input = element('input'); input.id = 'size-input-' + (++counter);
    input.type = number ? 'number' : 'text'; input.value = value ?? '';
    input.readOnly = readonly;
    if (number) { input.min = min; input.step = '1'; input.inputMode = 'numeric'; }
    else { input.setAttribute('list', 'size-options'); input.maxLength = 80; }
    if (max !== undefined) input.max = max;
    const title = element('label', label); title.htmlFor = input.id;
    field.append(title, input); return {field, input};
  };
  const post = async body => {
    const response = await fetch(window.location.href, {
      method: 'POST', body, credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest'},
    });
    let result;
    try { result = await response.json(); }
    catch { throw new Error('Penyimpanan belum berhasil. Periksa koneksi atau sesi masuk, lalu coba kembali. Draft tetap tersedia.'); }
    if (!response.ok) throw new Error(result.errors?.join(' ') || 'Anda tidak memiliki izin untuk menyimpan.');
    return result;
  };
  const busy = (form, saving) => {
    form.setAttribute('aria-busy', String(saving));
    form.querySelectorAll('input,select,button').forEach(control => {
      if (saving) { control.dataset.disabledBefore = String(control.disabled); control.disabled = true; }
      else control.disabled = control.dataset.disabledBefore === 'true';
    });
    const submit = form.querySelector('[type=submit]');
    if (saving) { submit.dataset.labelBefore = submit.textContent; submit.textContent = 'Menyimpan…'; }
    else submit.textContent = submit.dataset.labelBefore;
  };
  const showNotice = text => { notice.textContent = text; notice.hidden = false; };
  const renderHistory = shipments => {
    const history = document.getElementById('shipment-history'); history.replaceChildren();
    for (const shipment of shipments) {
      const row = element('tr');
      for (const value of [shipment.date, shipment.material, shipment.color]) row.append(element('td', value));
      const count = element('td', shipment.pcs + ' pcs', 'num');
      count.append(element('small', shipment.sizes.length ? shipment.sizes.map(s => s.label + ' ' + s.pcs).join(' · ') :
        'Kiriman lama belum dirinci per ukuran', 'cell-note')); row.append(count);
      for (const value of [shipment.gudang, shipment.surat_jalan, shipment.catatan]) row.append(element('td', value));
      history.append(row);
    }
    if (!shipments.length) { const row = element('tr'), cell = element('td', 'Belum ada kiriman gudang yang dicatat.', 'empty'); cell.colSpan = 7; row.append(cell); history.append(row); }
  };
  const updateState = result => {
    items = result.items;
    document.querySelectorAll('[data-po-total]').forEach(node => { node.textContent = result.summary[node.dataset.poTotal]; });
    const header = document.querySelector('[data-po-status]');
    if (header) header.replaceChildren(element('span', result.summary.label, 'status ' + result.summary.code));
    const missing = document.querySelector('[data-results-missing]');
    if (missing) {
      missing.hidden = !result.missing_count;
      missing.textContent = 'Hasil ' + result.missing_count + ' pasangan bahan / warna belum dicatat. Isi rincian ukuran agar status pengiriman lengkap.';
    }
    for (const row of result.rows) {
      const main = mainFor(row.row_key); if (!main) continue;
      const data = itemFor(row.row_key);
      main.querySelector('[data-item-total]').textContent = row.hasil === null ? 'Belum diisi' : row.hasil + ' pcs';
      const summary = data.sizes.slice(0, 3).map(s => s.label + ' ' + s.pcs).join(' · ');
      main.querySelector('[data-size-summary]').textContent = (summary || 'Ukuran belum diisi') +
        (data.sizes.length > 3 ? ' · +' + (data.sizes.length - 3) + ' ukuran' : '');
      main.querySelector('[data-item-shipped]').textContent = row.shipped;
      main.querySelector('[data-item-remaining]').textContent = row.remaining ?? '—';
      main.querySelector('[data-item-status]').replaceChildren(element('span', row.status_label, 'status ' + row.status_key));
      main.querySelector('[data-item-usage]').textContent = row.pemakaian === null ? '—' : Number(row.pemakaian).toLocaleString('id-ID', {minimumFractionDigits: 2});
      main.querySelector('[data-legacy-size-note]').hidden = !data.pending.length;
      const toggle = toggleFor(row.row_key);
      if (toggle && openKey !== row.row_key) toggle.textContent = data.sizes_complete ? 'Rincian ukuran' : 'Isi ukuran';
    }
    renderHistory(result.shipments);
    renderShipping();
  };
  const close = key => {
    if (!key) return;
    panelFor(key).hidden = true;
    const toggle = toggleFor(key); toggle.setAttribute('aria-expanded', 'false');
    toggle.textContent = itemFor(key).sizes_complete ? 'Rincian ukuran' : 'Isi ukuran';
    if (openKey === key) openKey = null;
  };
  const finish = (key, result, text) => {
    updateState(result); clearDirty(key);
    panelFor(key).querySelector('[data-editor-host]').replaceChildren();
    delete panelFor(key).dataset.ready;
    if (openKey === key) { close(key); toggleFor(key).focus(); }
    showNotice(text);
  };
  const renderEditor = key => {
    const panel = panelFor(key), host = panel.querySelector('[data-editor-host]');
    host.replaceChildren(); panel.dataset.ready = 'true';
    const data = itemFor(key), toggle = toggleFor(key);
    const canEdit = host.dataset.canEdit === 'true';
    host.append(element('h3', 'Ukuran · ' + toggle.dataset.context));
    if (!canEdit) {
      if (!data.sizes.length) host.append(element('p', 'Ukuran belum diisi.', 'field-help'));
      for (const size of data.sizes) host.append(element('p', size.label + ': hasil ' + size.pcs + ' pcs · terkirim ' +
        (size.shipped ?? 'Belum dirinci') + ' · sisa ' + (size.remaining ?? 'Belum dirinci'), 'size-read-row'));
      if (data.pending.length) host.append(element('p', 'Kiriman lama belum dirinci per ukuran.', 'field-help'));
      return;
    }
    const form = element('form'); form.method = 'post';
    const hint = element('p', 'Isi total hasil untuk setiap ukuran. Angka baru mengganti hasil ukuran sebelumnya.', 'field-help');
    if (data.id && !data.sizes_complete) hint.textContent += ' Pembagian pertama harus berjumlah ' + data.pcs + ' pcs sesuai hasil lama.';
    if (data.pending.length) hint.textContent += ' Total hasil tetap sampai kiriman lama selesai dirinci.';
    const grid = element('div', undefined, 'size-input-grid');
    const total = element('strong', '0 pcs');
    const totalLine = element('p', 'Total hasil: ', 'size-total'); totalLine.append(total);
    const error = alertBox();
    const rows = () => [...grid.querySelectorAll('[data-size-row]')];
    const updateTotal = () => {
      const values = rows().map(row => row.querySelector('[data-size-pcs]').value);
      total.textContent = values.every(value => /^\d+$/.test(value)) ? values.reduce((sum, value) => sum + Number(value), 0) + ' pcs' : 'Lengkapi jumlah hasil';
    };
    const appendSize = size => {
      const row = element('div', undefined, 'size-input-row'); row.dataset.sizeRow = ''; row.dataset.id = size.id ?? '';
      const label = inputField('Ukuran', size.label, {readonly: size.history}); label.input.dataset.sizeLabel = ''; label.input.required = true;
      const count = inputField('Hasil produksi (pcs)', size.pcs, {number: true, min: size.shipped ?? 0}); count.input.dataset.sizePcs = ''; count.input.required = true;
      const remove = button('Hapus', 'link-button size-remove'); remove.disabled = Boolean(size.history);
      remove.setAttribute('aria-label', 'Hapus ukuran ' + (size.label || 'draft'));
      remove.addEventListener('click', () => { row.remove(); markDirty(key); updateTotal(); });
      row.append(label.field, count.field, remove);
      if (size.id) row.append(element('small', 'Terkirim: ' + (size.shipped === null ? 'Belum dirinci' : size.shipped + ' pcs') +
        ' · sisa: ' + (size.remaining === null ? 'Belum dirinci' : size.remaining + ' pcs'), 'field-help size-balance'));
      grid.append(row); return label.input;
    };
    (data.sizes.length ? data.sizes : [{label: '', pcs: ''}]).forEach(appendSize); updateTotal();
    grid.addEventListener('input', () => { markDirty(key); updateTotal(); });
    const add = button('Tambah ukuran');
    add.addEventListener('click', () => { const input = appendSize({label: '', pcs: ''}); markDirty(key); updateTotal(); input.focus(); });
    const cancel = button('Batal');
    cancel.addEventListener('click', () => { clearDirty(key); renderEditor(key); close(key); toggle.focus(); });
    const submit = element('button', 'Simpan ukuran', 'button primary'); submit.type = 'submit';
    const actions = element('div', undefined, 'size-actions'); actions.append(add, cancel, submit);
    form.append(hint, grid, totalLine, error, actions); host.append(form);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (form.getAttribute('aria-busy') === 'true') return;
      if ([...host.querySelectorAll('[data-size-id]')].some(input => input.value !== '')) {
        error.textContent = 'Simpan atau batalkan draft rekonsiliasi terlebih dahulu.';
        error.hidden = false; error.focus(); return;
      }
      const body = new FormData(); body.set('action', 'sizes'); body.set('material', toggle.dataset.material); body.set('color', toggle.dataset.color);
      if (data.id) body.set('hasil_id', data.id);
      body.set('sizes', JSON.stringify(rows().map(row => ({id: row.dataset.id || null,
        label: row.querySelector('[data-size-label]').value, pcs: row.querySelector('[data-size-pcs]').value}))));
      error.hidden = true; busy(form, true);
      try { finish(key, await post(body), 'Ukuran berhasil disimpan.'); }
      catch (failure) { error.textContent = failure.message; error.hidden = false; markDirty(key); error.focus(); }
      finally { busy(form, false); }
    });
    if (data.sizes_complete && data.pending.length) {
      const reconciliation = element('form', undefined, 'size-reconciliation');
      reconciliation.append(element('h3', 'Lengkapi kiriman lama'), element('p',
        'Gunakan catatan ukuran yang dapat diverifikasi. Alokasi ini merinci kiriman tersimpan dan mempertahankan total terkirim.', 'field-help'));
      for (const shipment of data.pending) {
        const group = element('fieldset'); group.dataset.shipmentId = shipment.id;
        group.append(element('legend', 'Kiriman #' + shipment.id + ' · ' + shipment.date + ' · ' + shipment.pcs + ' pcs' +
          (shipment.surat_jalan ? ' · ' + shipment.surat_jalan : '')));
        const fields = element('div', undefined, 'size-ship-grid');
        for (const size of data.sizes) {
          const field = inputField(size.label + ' (pcs)', '', {number: true, max: size.pcs});
          field.input.dataset.sizeId = size.id; fields.append(field.field);
        }
        group.append(fields); reconciliation.append(group);
      }
      const recError = alertBox(), recSubmit = element('button', 'Simpan rekonsiliasi', 'button'); recSubmit.type = 'submit';
      const recCancel = button('Batal rekonsiliasi');
      recCancel.addEventListener('click', () => { reconciliation.reset(); markDirty(key); });
      const recActions = element('div', undefined, 'size-actions'); recActions.append(recCancel, recSubmit);
      reconciliation.append(recError, recActions); host.append(reconciliation);
      reconciliation.addEventListener('input', () => markDirty(key));
      reconciliation.addEventListener('submit', async event => {
        event.preventDefault(); if (reconciliation.getAttribute('aria-busy') === 'true') return;
        if (JSON.parse(editorSnapshot(key)).sizes.some((row, index) =>
            JSON.stringify(row) !== JSON.stringify(JSON.parse(host.dataset.baseline).sizes[index])) ||
            rows().length !== JSON.parse(host.dataset.baseline).sizes.length) {
          recError.textContent = 'Simpan atau batalkan perubahan hasil ukuran terlebih dahulu.';
          recError.hidden = false; recError.focus(); return;
        }
        const body = new FormData(); body.set('action', 'reconcile_sizes'); body.set('hasil_id', data.id);
        body.set('shipments', JSON.stringify([...reconciliation.querySelectorAll('fieldset')].map(group => ({
          shipment_id: group.dataset.shipmentId, sizes: [...group.querySelectorAll('[data-size-id]')].map(input => ({id: input.dataset.sizeId, pcs: input.value})),
        }))));
        recError.hidden = true; busy(reconciliation, true);
        try { finish(key, await post(body), 'Kiriman lama berhasil dirinci. Total terkirim tetap.'); }
        catch (failure) { recError.textContent = failure.message; recError.hidden = false; markDirty(key); recError.focus(); }
        finally { busy(reconciliation, false); }
      });
    }
    host.dataset.baseline = editorSnapshot(key);
  };
  toggles.forEach(toggle => toggle.addEventListener('click', () => {
    const key = toggle.dataset.sizeToggle;
    if (openKey === key) { close(key); return; }
    close(openKey); openKey = key;
    const panel = panelFor(key); if (!panel.dataset.ready) renderEditor(key);
    panel.hidden = false; toggle.setAttribute('aria-expanded', 'true'); toggle.textContent = 'Tutup rincian';
  }));
  const renderShipping = (reset = false) => {
    if (!shipForm) return;
    const selected = shipColor.selectedOptions[0];
    for (const option of shipColor.options) {
      if (!option.value) continue;
      const matches = option.dataset.material === shipMaterial.value;
      option.hidden = !matches; option.disabled = !matches || option.dataset.unmapped === 'true';
      option.textContent = option.dataset.colorLabel + (option.dataset.unmapped === 'true' ? ' · tentukan bahan data lama' : '');
    }
    shipColor.disabled = !shipMaterial.value;
    if (reset || selected?.dataset.material !== shipMaterial.value) shipColor.selectedIndex = 0;
    const option = shipColor.selectedOptions[0]; shippingKey = option?.dataset.rowKey ?? null;
    const data = itemFor(shippingKey), fields = shipForm.querySelector('[data-shipment-size-fields]'); fields.replaceChildren();
    shipForm.querySelector('[name=hasil_id]').value = data.id ?? '';
    const context = document.getElementById('ship-context'), submit = shipForm.querySelector('[type=submit]');
    submit.disabled = true;
    if (!shippingKey) context.textContent = 'Pilih bahan dan warna untuk melihat ukuran dan sisa kirim.';
    else if (option.dataset.unmapped === 'true') context.textContent = 'Tentukan bahan data lama terlebih dahulu.';
    else if (!data.sizes_complete) context.textContent = 'Isi rincian ukuran hasil produksi item ini terlebih dahulu.';
    else if (!data.shipments_complete) context.textContent = 'Kiriman lama belum dirinci per ukuran. Lengkapi kiriman lama melalui Rincian ukuran.';
    else {
      context.textContent = 'Isi jumlah untuk ukuran yang dikirim. Ukuran lain boleh kosong atau 0.';
      const grid = element('div', undefined, 'size-ship-grid');
      const draft = shippingDrafts.get(shippingKey) || {};
      for (const size of data.sizes) {
        const field = inputField(size.label + ' · sisa ' + size.remaining + ' pcs', draft[size.id] ?? '',
          {number: true, max: Math.max(size.remaining, 0)});
        field.input.dataset.shipSizeId = size.id; field.input.disabled = size.remaining <= 0;
        grid.append(field.field);
      }
      fields.append(grid); submit.disabled = !data.sizes.some(size => size.remaining > 0);
    }
    updateShippingTotal();
  };
  const updateShippingTotal = () => {
    if (!shipForm) return;
    const inputs = [...shipForm.querySelectorAll('[data-ship-size-id]')];
    shipForm.querySelector('[data-shipment-total]').textContent = inputs.reduce((sum, input) => sum + (Number(input.value) || 0), 0) + ' pcs';
  };
  shipMaterial?.addEventListener('change', () => renderShipping(true));
  shipColor?.addEventListener('change', () => renderShipping());
  shipForm?.addEventListener('input', event => {
    if (event.target.dataset.shipSizeId) {
      const draft = shippingDrafts.get(shippingKey) || {}; draft[event.target.dataset.shipSizeId] = event.target.value;
      shippingDrafts.set(shippingKey, draft); updateShippingTotal();
    }
  });
  shipForm?.addEventListener('submit', async event => {
    event.preventDefault(); if (shipForm.getAttribute('aria-busy') === 'true') return;
    const key = shippingKey, body = new FormData(shipForm);
    body.set('sizes', JSON.stringify([...shipForm.querySelectorAll('[data-ship-size-id]')].filter(input => !input.disabled)
      .map(input => ({id: input.dataset.shipSizeId, pcs: input.value}))));
    const error = shipForm.querySelector('[data-shipment-error]'); error.hidden = true; busy(shipForm, true);
    try {
      const result = await post(body); shippingDrafts.delete(key);
      updateState(result); shipForm.querySelector('[name=request_id]').value = result.request_id;
      showNotice('Kiriman gudang berhasil disimpan.');
      // Refresh saved balances in unopened editors while retaining any unsaved drafts.
      for (const toggle of toggles) {
        const itemKey = toggle.dataset.sizeToggle;
        if (!dirty.has(itemKey)) {
          if (openKey === itemKey) renderEditor(itemKey);
          else delete panelFor(itemKey).dataset.ready;
        }
      }
    } catch (failure) { error.textContent = failure.message; error.hidden = false; error.focus(); }
    finally { busy(shipForm, false); renderShipping(); }
  });
  if (shipForm) renderShipping();
});
