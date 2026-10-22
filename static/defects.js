document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('bs-form');
  if (!form) return;
  const invoices = JSON.parse(document.getElementById('bs-invoice-impacts').textContent);
  const rows = [...form.querySelectorAll('[data-bs-row]')];
  // Integer arithmetic matches the server: six decimal yards, two decimal rupiah,
  // then HALF_UP to cents for each item before summing invoice adjustments.
  const scaled = (raw, digits) => {
    const text = String(raw).trim().replace(',', '.');
    if (!new RegExp(`^\\d+(?:\\.\\d{1,${digits}})?$`).test(text)) return null;
    const [whole, part = ''] = text.split('.');
    return BigInt(whole + part.padEnd(digits, '0'));
  };
  const money = cents => {
    const digits = cents.toString().padStart(3, '0');
    return `Rp ${digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, '.')},${digits.slice(-2)}`;
  };
  const max = (a, b) => a > b ? a : b;
  const update = () => {
    const totals = new Map();
    rows.forEach(row => {
      const input = row.querySelector('[data-bs-yard]');
      const selected = row.querySelector('[data-bs-select]').checked;
      input.required = selected;
      input.setCustomValidity('');
      const yard = scaled(input.value, 6);
      const available = scaled(row.dataset.available, 6);
      const price = scaled(row.dataset.price, 2);
      if (selected && (!yard || yard > available))
        input.setCustomValidity('Isi yard BS positif, maksimal sisa layak pakai, dengan hingga 6 desimal.');
      if (selected && !price) input.setCustomValidity('Isi Harga per Yard di Master Bahan dahulu.');
      const amount = yard && price ? (yard * price + 500000n) / 1000000n : 0n;
      row.querySelector('[data-bs-value]').textContent = amount ? money(amount) : '—';
      row.classList.toggle('selected-row', selected);
      if (selected && amount) totals.set(row.dataset.invoice,
        (totals.get(row.dataset.invoice) || 0n) + amount);
    });
    const panel = document.getElementById('bs-impact');
    panel.replaceChildren();
    if (!totals.size) panel.textContent = 'Pilih roll dan isi yard BS untuk melihat dampak pada invoice asal.';
    totals.forEach((amount, id) => {
      const invoice = invoices[id];
      const net = scaled(invoice.net, 2), paid = scaled(invoice.paid, 2);
      const p = document.createElement('p');
      if (invoice.reconciliation) p.textContent = `Invoice ${invoice.number} perlu rekonsiliasi sebelum BS dapat disimpan.`;
      else if (amount > net) p.textContent = `Nilai BS ${money(amount)} melampaui tagihan invoice ${invoice.number}. Periksa harga atau yard.`;
      else {
        const after = net - amount, remaining = max(after - paid, 0n);
        const refund = max(paid - after, 0n);
        p.textContent = `Invoice ${invoice.number} · ${invoice.vendor}: BS ${money(amount)}. ` +
          `Sisa tagihan dari ${money(max(net - paid, 0n))} menjadi ${money(remaining)}. ` +
          (refund ? `Total refund yang perlu diminta pada invoice ini ${money(refund)}.` : 'Tidak ada kebutuhan refund.');
      }
      panel.append(p);
    });
  };
  rows.forEach(row => {
    row.querySelector('[data-bs-all]').addEventListener('click', () => {
      row.querySelector('[data-bs-select]').checked = true;
      row.querySelector('[data-bs-yard]').value = row.dataset.available.replace('.', ',');
      update();
    });
    row.querySelector('[data-bs-yard]').addEventListener('input', () => {
      if (row.querySelector('[data-bs-yard]').value) row.querySelector('[data-bs-select]').checked = true;
      update();
    });
    row.querySelector('[data-bs-select]').addEventListener('change', update);
  });
  form.addEventListener('submit', event => {
    if (!rows.some(row => row.querySelector('[data-bs-select]').checked)) {
      event.preventDefault();
      document.getElementById('bs-impact').textContent = 'Pilih setidaknya satu roll untuk dicatat BS.';
    }
  }, true);
  update();
});
