document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-payment-form]');
  if (!form) return;
  const kind = form.querySelector('[name="kind"]');
  const amount = form.querySelector('[name="amount"]');
  const seen = form.querySelector('[name="remaining_seen"]');
  const after = form.querySelector('[data-payment-after]');
  const warning = form.querySelector('[data-payment-warning]');
  const cents = raw => {
    let value = String(raw).trim();
    if (/^\d{1,3}(\.\d{3})+(,\d{1,2})?$/.test(value)) {
      value = value.replaceAll('.', '').replace(',', '.');
    } else if (/^\d+([.,]\d{1,2})?$/.test(value)) {
      value = value.replace(',', '.');
    } else return null;
    const [whole, fraction = ''] = value.split('.');
    return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'));
  };
  const rupiah = value => {
    const whole = (value / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    return `${whole},${(value % 100n).toString().padStart(2, '0')}`;
  };
  const remaining = cents(form.dataset.remaining);
  const preview = () => {
    const paid = cents(amount.value);
    if (remaining === null || paid === null || paid <= 0n) {
      after.textContent = '—';
      warning.textContent = amount.value ? 'Isi nominal yang valid dan lebih besar dari nol.' : '';
    } else if (paid > remaining) {
      after.textContent = '—';
      warning.textContent = 'Nominal melebihi sisa tagihan. Tinjau ulang pembayaran.';
    } else {
      after.textContent = `Rp ${rupiah(remaining - paid)}`;
      warning.textContent = kind.value === 'lunas' && paid !== remaining ?
        'Sisa telah berubah. Tinjau ulang nominal dan bukti, lalu buka ulang form.' : '';
    }
  };
  kind.addEventListener('change', () => {
    amount.readOnly = kind.value === 'lunas';
    if (kind.value === 'lunas' && remaining !== null) {
      amount.value = rupiah(remaining);
      seen.value = rupiah(remaining);
    } else amount.value = '';
    preview();
    if (!amount.readOnly) amount.focus();
  });
  amount.addEventListener('input', preview);
  amount.addEventListener('blur', () => {
    const parsed = cents(amount.value);
    if (parsed !== null) amount.value = rupiah(parsed);
    preview();
  });
  preview();
});
