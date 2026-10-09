# Payment Invoice

Implementasi PRD Payment Invoice tanggal 9 Oktober 2026 pada Django existing.

## Perilaku

- **Payment** menjadi menu terakhir sidebar bagi semua pengguna yang login.
- Sumber tagihan adalah `Invoice` existing, satu baris per ID invoice. Alokasi
  bahan, warna, maupun beberapa PO tidak membuat tagihan tambahan.
- Invoice baru wajib memiliki total positif. Input `500.000.000` dibaca sebagai
  Rp500 juta; nominal dihitung menggunakan Decimal dengan dua angka desimal.
- Daftar menyediakan pencarian invoice/vendor, status, Periode Invoice,
  pagination, dan **Download Data**. Detail memperlihatkan file invoice, posisi
  kumulatif, progress bar, dan riwayat beserta bukti setiap transaksi.
- Hanya role `direktur` yang aktif dapat mencatat **Lunas/Cicil**. Admin dan
  superuser tidak mendapat hak ini. Role **Accounting** tersedia pada pengelolaan
  akun, membuka Payment setelah login, dan hanya dapat melihat/mengunduh.
- Lunas menggunakan seluruh sisa yang sudah ditinjau. Jika sisa berubah sebelum
  penyimpanan, server menolak dan menampilkan nominal terbaru. Cicil diterima
  hanya jika positif dan tidak melebihi sisa terbaru.
- Total dibayar, sisa, progres, dan status berasal dari transaksi tersimpan.
  Status adalah Unpaid, Dicicil, atau Paid. Selama sisa positif, progres tampilan
  maksimal 99,9%; hanya sisa nol menampilkan 100%.
- Setiap transaksi menyimpan bukti sendiri di database, mengikuti pola lampiran
  invoice. PDF/JPG/JPEG/PNG diperiksa melalui ekstensi, signature, dan ukuran;
  batas bukti pembayaran 5 MB. Lampiran invoice tetap memakai batas existing 10 MB.
- Bukti dibuka/diunduh melalui endpoint yang memerlukan login. Tidak ada endpoint
  edit/hapus pembayaran. Setelah pembayaran ada, total, vendor, nomor, dan file
  invoice terkunci, serta pembatalan invoice ditolak. Detail nonfinansial tetap
  mengikuti aturan existing.
- Pembayaran tidak mengubah status atau penyelesaian PO.

## Konsistensi transaksi

`InvoicePayment` berelasi langsung ke invoice, dengan request ID unik dan fingerprint
yang mencakup identitas invoice/user, nominal, tanggal, pilihan, dan bukti.
Pengiriman ulang identik mengembalikan transaksi pertama, termasuk setelah Paid;
penggunaan request ID yang sama untuk data berbeda ditolak.

Pencatatan mengambil lock invoice sebelum menghitung sisa terbaru. PostgreSQL
menggunakan `SELECT FOR UPDATE`; SQLite memperoleh write lock sebelum membaca
saldo. Edit nominal dan pembatalan invoice memakai lock yang sama. Bukti, transaksi,
dan log tersimpan dalam satu transaksi database sehingga kegagalan tidak meninggalkan
bukti lepas atau menambah total dibayar. Konflik mempertahankan input yang dapat
dipulihkan; berkas perlu dipilih ulang oleh pengguna.

## Excel

File `.xlsx` berisi tepat dua sheet: **Rekap Invoice** dan **Riwayat Pembayaran**.
Metadata filter serta waktu ekspor WIB berada di atas header pada baris 5. Angka
disimpan sebagai sel numerik, tanggal invoice terpisah dari tanggal pembayaran,
dan tautan bukti mengarah ke endpoint aplikasi yang memerlukan login. Identifier
yang dimasukkan pengguna disimpan sebagai teks Excel untuk mencegah formula.

Invoice terpilih dikunci ketika data kedua sheet dikumpulkan. Rekap memakai fungsi
perhitungan yang sama dengan halaman dan riwayat memuat semua transaksi invoice
terpilih. Ekspor kosong tetap memiliki dua sheet dan header yang valid.

## Migrasi dan invoice lama

Migrasi `0009_invoice_payments` menambah ledger pembayaran dan role Accounting,
tanpa mengganti autentikasi, database, maupun hosting.

Repository tidak memiliki riwayat pembayaran lama yang dapat diverifikasi.
Migrasi menandai seluruh invoice existing `payment_reconciled=False`; invoice
baru memakai nilai True. Invoice lama tampil **Perlu rekonsiliasi**, dengan total
dibayar/sisa/progres belum terverifikasi dan sel ekspor kosong. Tidak ada transaksi,
bukti, atau status Unpaid fiktif yang dibuat. Mengubah detail invoice lama tidak
otomatis memverifikasi posisi keuangannya. Rekonsiliasi dan koreksi finansial perlu
memakai data yang disetujui dan rancangan tersendiri; V1 tidak menyediakan aksi
untuk mengubah histori keuangan.

Migrasi sudah diterapkan ke `severli.sqlite3` lokal setelah audit salinan dan backup.
Seluruh data dari 19 tabel operasional existing tetap identik. Ada 1 invoice lama
yang perlu rekonsiliasi, 0 invoice tanpa nominal positif, dan 0 pembayaran historis
yang dibuat. Backup dan laporan tersimpan di:

- `.verification/payment-before-20261009T025703775292Z.sqlite3`
- `.verification/payment-migration-20261009T025703775292Z.json`

Untuk lingkungan lain, terapkan `python manage.py migrate --noinput` pada schema
aktif existing. Implementasi ini belum dideploy ke layanan produksi.

## Verifikasi

Tanggal 9 Oktober 2026:

- 136 tes: 128 lulus dan 8 tes PostgreSQL existing dilewati karena PostgreSQL tes
  terpisah tidak tersedia. Tes baru Payment mencakup cicilan Rp500 juta + Rp300 juta
  + pelunasan Rp200 juta, seluruh role, nominal/bukti/tanggal invalid, retry,
  konflik Lunas, Paid, penguncian invoice, rollback, filter, dan isi Excel.
- Tiga tes request bersamaan lulus pada SQLite: dua cicilan yang berpotensi lebih
  bayar, request identik, serta Lunas bersamaan dengan cicilan.
- Django system check, Ruff, dan pemeriksaan migrasi tanpa perubahan lulus.
- 12 halaman/ekspor pada data lokal nyata lulus dengan koneksi SQLite read-only
  untuk Purchasing, Direktur, Admin, dan Accounting.
- Struktur responsive dan syntax JavaScript diperiksa. QA visual desktop/mobile
  belum dilakukan karena browser IAB dan Chrome tidak tersedia di runtime sesi.
  Penguncian dan konkurensi pada PostgreSQL belum diverifikasi di lingkungan ini.

Perintah verifikasi:

```powershell
$env:DEBUG = 'true'
$env:TEST_DATABASE_URL = ''
./.venv/Scripts/python.exe manage.py test tracking
./.venv/Scripts/python.exe manage.py check
./.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
./.venv/Scripts/python.exe scripts/audit_payment_migration.py --database severli.sqlite3
./.venv/Scripts/python.exe scripts/smoke_payment_pages.py --database severli.sqlite3
```

Script audit hanya menjalankan migrasi pada salinan, kecuali `--apply` diberikan.
Opsi apply tetap membuat backup dan membandingkan data terlebih dahulu. Untuk
memverifikasi PostgreSQL, isi `TEST_DATABASE_URL` dengan database tes terpisah.
