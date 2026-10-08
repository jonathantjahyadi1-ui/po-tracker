# Ukuran per warna pada PO Tracker

Fitur mengikuti PRD Ukuran per Warna versi 1.0, 7 Oktober 2026.

## Pemetaan ke aplikasi

- Item produksi adalah ID `Hasil` existing. Keunikan existing tetap PO + bahan + warna;
  sumber invoice/alokasi tetap ditampilkan. Fitur ini tidak memecah atau menggabungkan
  item berdasarkan label bahan/warna. Ukuran dari PO atau item lain ditolak.
- `HasilUkuran` menyimpan label, kunci normalisasi, hasil pcs dan urutan pengguna.
  Ukuran custom/angka diperbolehkan. Spasi dirapikan dan kunci memakai kapitalisasi;
  OS dan All Size tetap berbeda. Total `Hasil.pcs` diperbarui secara atomik sebagai
  cache untuk pemakaian existing; resolver status membaca jumlah rincian ukuran.
- `KirimGudang` tetap satu transaksi dengan tanggal, gudang, surat jalan, catatan,
  ID dan identitas retry yang sama. `KirimUkuran` menghubungkan transaksi ke ukuran
  dari ID Hasil yang benar. Tidak ada transaksi nol atau ukuran lama yang ditebak.
- `Hasil.sizes_complete` menandai pembagian hasil. Transaksi tanpa rincian ukuran
  menandai kiriman historis yang belum direkonsiliasi. Saldo lama tetap dihitung dari
  transaksi existing dan tidak dijumlahkan lagi dengan rincian kirimannya.
- Purchasing tetap mengisi hasil/kiriman/rekonsiliasi. Direktur melihat rincian.
  Pembatasan Admin dan akses modul lain mengikuti aturan existing. Audit memakai
  `Log`, berisi pelaku, waktu, nilai sebelum/sesudah dan ID item/transaksi.
- Tidak ada mekanisme pembatalan kiriman pada aplikasi existing; fitur ini tidak
  menambahkan penghapusan histori. Label dan ID ukuran yang mempunyai histori
  kiriman dilindungi dan referensi database memakai PROTECT.

## Migrasi

Migrasi tambahan: `0008_production_sizes`. Tidak mengubah migrasi 0004–0007 dan
menambahkan struktur tanpa mengubah total atau transaksi lama. Migrasi ulang
melalui Django aman karena ledger migrasi; tidak ada pembagian otomatis.

Untuk deployment pada database aktif, buat backup dan audit salinan dahulu.
Gunakan schema aktif yang sama serta konfigurasi database existing:

```powershell
./.venv/Scripts/python.exe scripts/audit_local_migrations.py
./.venv/Scripts/python.exe manage.py migrate
./.venv/Scripts/python.exe manage.py check
./.venv/Scripts/python.exe manage.py audit_workflow --json
```

Script audit menggunakan backup SQLite yang konsisten dan koneksi sumber
read-only; hasilnya ada di `.verification/migration-conservation.json`.
Database `local.sqlite3` dengan schema aplikasi lain tetap dicatat sebagai
pengecualian, tanpa dipaksakan melalui migrasi aplikasi ini.

## Cara menggunakan

1. Buka PO → CMT → nomor PO → Hasil produksi dan sisa kirim.
2. Klik **Isi ukuran** atau **Rincian ukuran** pada bahan/warna. Masukkan total
   hasil untuk setiap ukuran, tambah/hapus baris yang belum mempunyai histori,
   lalu **Simpan ukuran**. Angka baru mengganti total ukuran sebelumnya.
3. Draft tetap tersedia saat rincian ditutup atau item lain dibuka. Penanda
   **Belum disimpan** menunjukkan draft; **Batal** mengembalikan nilai tersimpan.
   Respons server memperbarui total, ringkasan dan status. Error mempertahankan draft.
4. Pada **Catat kiriman ke gudang**, pilih bahan dan warna, lalu isi kiriman pada
   ukuran yang diperlukan. Ukuran lain boleh kosong atau 0. Total otomatis.
   Simpan satu transaksi; seluruh rincian ditolak jika satu ukuran melebihi sisa.

## Rekonsiliasi historis

- Data lama tanpa ukuran tetap menampilkan total hasil existing dan **Ukuran belum
  diisi**. Pembagian pertama harus sama dengan total lama, termasuk hasil nol.
  Koreksi total dilakukan melalui rincian ukuran setelah pembagian tersimpan.
- Untuk bahan legacy yang belum dipetakan, selesaikan **Tentukan bahan untuk data
  lama** existing terlebih dahulu. Jangan mengalokasikan total ke setiap bahan.
- Jika ada kiriman lama, simpan pembagian hasil sesuai total lama dahulu. Terkirim
  dan sisa setiap ukuran akan tertulis **Belum dirinci**; angka agregat dan status
  PO lama tetap dipertahankan. Kiriman baru serta perubahan total hasil item
  diblokir sampai rekonsiliasi selesai; item lain tetap dapat diproses.
- Buka rincian item, lalu **Lengkapi kiriman lama**. Untuk setiap transaksi yang
  ditampilkan, isi pembagian berdasarkan catatan yang dapat diverifikasi. Total
  tiap pembagian harus tepat sama dengan pcs transaksi, seluruh transaksi belum
  dirinci harus dilengkapi sekaligus, dan akumulasi ukuran tidak boleh melebihi hasil.
  Klik **Simpan rekonsiliasi**. Ini menambah rujukan ukuran, bukan transaksi baru.
- Penyimpanan atomik dan retry dengan pembagian yang sama tidak menggandakan
  kiriman, saldo, ataupun audit. Pembagian tersimpan yang berbeda ditolak.
- Bila catatan ukuran tidak tersedia, biarkan belum dirinci. Tidak ada ukuran
  All Size otomatis, tanggal/nomor kiriman fiktif, atau alokasi perkiraan.

## Verifikasi

```powershell
./.venv/Scripts/python.exe manage.py test tracking
./.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
./.venv/Scripts/python.exe -m ruff check tracking scripts/audit_local_migrations.py
node --check static/production-sizes.js
```

Tes regresi lama memilih All Size secara eksplisit hanya sebagai fixture tes;
aturan aplikasi dan migrasi tidak membuat asumsi tersebut. Tes fitur mencakup
rincian hasil, batas setiap ukuran, atomisitas, retry, izin, isolasi ID,
rekonsiliasi, status lama serta migrasi yang mempertahankan histori.

Penguncian pada kiriman, hasil dan rekonsiliasi memakai transaksi dengan kunci
baris PO existing. Tes PostgreSQL memeriksa dua pengiriman sisa terakhir dan
retry bersamaan. SQLite tidak menyediakan kunci baris; tes ini dilewati di
SQLite. Jalankan pada PostgreSQL tes terpisah melalui TEST_DATABASE_URL.

QA visual desktop/mobile dan keyboard melalui browser nyata masih perlu
verifikasi karena sesi ini tidak menyediakan browser yang dapat dikendalikan.

## Hasil pemeriksaan, 8 Oktober 2026

- 101 tes dijalankan: 93 lulus, 8 tes khusus PostgreSQL dilewati di SQLite.
- Django check, makemigrations --check --dry-run, Ruff, pemeriksaan sintaks kedua
  berkas JavaScript, serta git diff --check lulus.
- Audit salinan database lokal lulus. Migrasi 0008 sudah diterapkan pada
  severli.sqlite3 setelah backup; semua baris lama pada 22 tabel tetap identik.
  Django menambahkan 2 content type dan 8 izin untuk kedua model baru.
- Backup sebelum migrasi: .verification/severli-before-0008-20261008-b4dd95b8.sqlite3.
  Laporan perbandingan: .verification/size-local-migration.json.
- Ukuran hasil dan rincian kiriman lama tidak dibuat otomatis. Data lokal lama
  hasil/terkirim tetap 1072/1072 pcs. Rekonsiliasi pengguna tetap diperlukan.
- 16 pemeriksaan halaman/ekspor sebagai Purchasing dan Direktur pada database
  aktif lulus melalui koneksi SQLite read-only; status lama tetap Done.
- QA visual browser, mobile dan keyboard belum dapat dijalankan karena tidak ada
  browser terhubung. Penguncian PostgreSQL juga belum diverifikasi pada lingkungan ini.

## Berkas yang berubah

- tracking/models.py; tracking/migrations/0008_production_sizes.py:
  struktur rincian dan penanda pembagian hasil lama.
- tracking/size_services.py; tracking/services.py:
  total ukuran, validasi, transaksi kiriman, retry, status dan rekonsiliasi.
- tracking/views.py; templates/po_detail.html:
  integrasi pada halaman existing, izin, respons penyimpanan dan histori ukuran.
- static/production-sizes.js; static/app.js; static/app.css:
  rincian yang dapat dibuka, draft, error, form kiriman dan aturan layar kecil.
- tracking/test_production_sizes.py; tracking/test_size_helpers.py;
  tracking/tests.py; tracking/test_service_safety.py:
  tes fitur dan penyesuaian fixture regresi dengan ukuran eksplisit.
- tracking/management/commands/seed_contoh.py:
  kompatibilitas data contoh lokal dengan input kiriman ukuran.
- scripts/audit_local_migrations.py:
  audit struktur baru pada salinan tanpa memodifikasi database sumber.
- README.md; docs/PRODUCTION_SIZES.md:
  petunjuk penggunaan, migrasi, rekonsiliasi dan batas verifikasi.
