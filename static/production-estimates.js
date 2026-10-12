document.addEventListener('DOMContentLoaded', () => {
  const usageSource = document.getElementById('material-usage-data');
  const allocationSource = document.getElementById('allocation-preview-data');
  if (!usageSource || !allocationSource) return;
  const usages = JSON.parse(usageSource.textContent);
  const allocations = JSON.parse(allocationSource.textContent);
  const exact = value => {
    const [whole, fraction = ''] = String(value).split('.');
    return BigInt(whole) * 100000000n + BigInt(fraction.padEnd(8, '0'));
  };
  const estimate = (yard, usage) => {
    const y = exact(yard), u = exact(usage), q = y / u;
    return String(q + ((2n * (y % u) >= u) ? 1n : 0n));
  };
  for (const form of document.querySelectorAll('[data-patent-allocation]')) {
    const select = form.querySelector('[name=product]');
    const preview = form.querySelector('[data-patent-preview]');
    const submit = form.querySelector('[type=submit]');
    const render = () => {
      preview.replaceChildren();
      const rows = allocations[form.dataset.patentAllocation] || [];
      let missing = !select.value || !rows.length;
      if (!select.value) preview.textContent = 'Pilih produk untuk melihat pemakaian paten dan estimasi.';
      else for (const row of rows) {
        const usage = usages.find(u => String(u.product_id) === select.value && u.material_id === row.material_id);
        const line = document.createElement('p'); line.className = 'field-help';
        line.textContent = row.material__name + ' / ' + row.color__name + ': ' +
          (usage ? 'Pemakaian Paten: ' + String(usage.usage_yard_per_pcs).replace('.', ',') +
            ' yard/pcs · Alokasi ' + String(row.yard).replace('.', ',') + ' yard · Estimasi ' + estimate(row.yard, usage.usage_yard_per_pcs) + ' pcs' :
            'Pemakaian paten untuk produk dan bahan ini belum diisi di Master.');
        preview.append(line); if (!usage) missing = true;
      }
      if (missing && select.value) {
        const link = document.createElement('a'); link.href = '/master/pemakaian-paten/?product=' + encodeURIComponent(select.value);
        link.textContent = 'Isi patokan di Master'; preview.append(link);
      }
      submit.disabled = missing;
    };
    select.addEventListener('change', render); render();
  }
});
