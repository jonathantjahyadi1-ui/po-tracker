# Severli PO Tracker

Pelacakan bahan dari invoice vendor, pengiriman dan penerimaan CMT, sampai hasil
produksi selesai dikirim ke gudang. Antarmuka berbahasa Indonesia.

## Lokal

Gunakan Python 3.12. Untuk menjalankan aplikasi di Windows, buka PowerShell
di folder proyek lalu jalankan:

```powershell
.\run-local.cmd
```

Buka `http://127.0.0.1:8765/login/` dan tekan `Ctrl+C` untuk menghentikan server.
Launcher ini menyiapkan virtual environment jika belum ada dan menjalankan migrasi.

Jika ingin menjalankan langkahnya secara manual:

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
./.venv/Scripts/python.exe manage.py migrate
./.venv/Scripts/python.exe manage.py createsuperuser
./.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8765
```

Admin dapat membuat akun purchasing dan direktur di halaman Akun. Untuk tampilan contoh,
jalankan `manage.py seed_contoh` saat `DEBUG=true`.

## Alur operasional

1. Di **Vendor → Tambah bahan**, isi metadata invoice dan hingga 10 panel Nama bahan.
   Setiap panel memiliki baris warna dengan lokasi opsional serta yard per roll.
   **Tambah baris** menambah warna; **Tambah nama bahan** menambah panel bahan.
   Tempel yard atau impor CSV/XLSX pada baris warna yang sesuai. Nomor PO belum diisi.
2. Cari nomor invoice di detail vendor, buka invoice, lalu pilih roll Tersedia dan
   CMT tujuan untuk mengajukan alokasi. Satu pengajuan dapat memuat beberapa
   bahan/warna dari satu invoice; roll menjadi Menunggu.
3. Di Alokasi, isi tanggal kirim kain dan gunakan **ACC dan tandai dikirim**.
   Seluruh roll dalam pengajuan langsung Dikirim. Penolakan wajib menyertakan alasan.
4. Buka CMT tujuan dan catat tanggal serta roll yang sudah diterima. Penerimaan
   sebagian diperbolehkan. Isi nomor PO setelah seluruh roll dalam pengiriman
   diterima; nama produk opsional juga dikelola di CMT.
5. Buka **PO → CMT → nomor PO**. Informasi PO hanya mengubah tanggal order.
   Isi total hasil produksi per pasangan bahan dan warna. Bahan berbeda dengan
   warna yang sama memiliki isian hasil dan sisa kirim masing-masing; angka baru
   mengganti total pasangan tersebut sebelumnya.
6. Tambah transaksi kiriman gudang. Hasil 200 pcs dan kiriman 150 pcs menghasilkan
   **Kurang kirim 50 pcs**. Tambahan 50 pcs menghasilkan **Done** jika setiap pasangan
   sudah lengkap dan seluruh hasilnya terkirim. Hasil yang dinaikkan menjadi 230 pcs
   membuat PO kembali **Kurang kirim 30 pcs**.

Kiriman baru tidak boleh melebihi sisa bahan/warna dan hasil tidak boleh dikurangi di
bawah total kiriman tercatat. Hasil kosong berbeda dari hasil nol. Kelebihan pada
data lama tetap ditampilkan sebagai anomali per bahan/warna; jumlah bersih antarpasangan
tidak dapat membuat PO Done. Tidak ada tombol selesai manual atau panel Roll PO.

File invoice PDF, JPG, atau PNG (maksimal 10 MB) tetap disimpan di database;
unduh serta penggantian lampiran tersedia pada invoice. Purchasing mengisi dan
melakukan ACC, Direktur melihat serta mengunduh, dan Admin mengelola akun serta
melihat riwayat. Hak akses juga diperiksa pada server.

Detail U-01 sampai U-09 dari PRD diterapkan sebagai **default implementasi yang
masih berupa usulan**, bukan keputusan yang sudah disetujui pengguna. Pilihan,
kompatibilitas data lama, dan hasil verifikasi dijelaskan di
[catatan implementasi](docs/PRD_IMPLEMENTATION.md).

## Variabel lingkungan

- `DEBUG`: `true` untuk lokal; `false` untuk produksi.
- `SECRET_KEY`: wajib di produksi.
- `DATABASE_URL`: URL PostgreSQL produksi. Tanpa URL, mode lokal memakai SQLite.
- `DATABASE_SCHEMA`: schema privat PostgreSQL untuk versi ini, misalnya `severli`.
- `LEGACY_USERS_SCHEMA`: opsional, schema akun lama yang perlu disalin saat deploy.
- `DATABASE_SSL`: `require` secara default; `disable` untuk PostgreSQL lokal.
- `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`: host dan origin aplikasi.
- `TEST_DATABASE_URL`: opsional untuk menjalankan tes di PostgreSQL terpisah.

## Deploy

`build.sh` membuat schema yang dikonfigurasi, menjalankan migrasi, lalu menyalin akun lama jika
`LEGACY_USERS_SCHEMA` diisi. Untuk layanan Render yang sudah ada, atur
`DATABASE_SCHEMA=severli` dan `LEGACY_USERS_SCHEMA=po_tracking` sebelum deploy;
schema `po_tracking` tetap tersimpan. Untuk database baru, kosongkan variabel legacy
dan buat admin dengan `python manage.py createsuperuser`.
Mekanisme penyalinan akun antar-schema ini tidak memindahkan data operasional
antar-schema; data operasional pada schema lama tetap berada di sana. Migrasi
revisi alur memetakan transaksi dalam **schema aktif yang sama**, mempertahankan
identitas roll, lampiran, hasil, kiriman, dan hubungan PO historis. Jangan mengganti
schema untuk menerapkan revisi pada data aktif.

Migrasi revisi mencakup `0004_material_cmt_workflow`, pemetaan histori pada
`0005_migrate_material_history`, dan ledger permintaan invoice pada
`0006_invoice_write_requests`. Ledger mencegah roll baru tergandakan saat POST
simpan invoice yang sama diulang.

`0007_production_material` memisahkan hasil dan kiriman berdasarkan bahan/warna.
Total lama otomatis dihubungkan ke bahan hanya jika PO/warna tersebut mempunyai
satu bahan yang diketahui. Total dengan beberapa kemungkinan bahan tetap utuh
pada baris **Bahan belum ditentukan**; Purchasing memilih bahan melalui detail PO
sebelum mengisi atau mengirim pasangan warna tersebut. Tidak ada penyalinan total
ke setiap bahan. Pilihan tidak dapat menimpa hasil atau kiriman yang sudah dicatat
untuk pasangan tujuan.

Sebelum rilis, jalankan migrasi pada salinan database aktif dan bandingkan jumlah
invoice, roll, alokasi, PO, total yard, hasil pcs, kiriman pcs, dan lampiran.
PO lama tanpa CMT atau dengan beberapa CMT tetap tersedia sebagai data historis;
pemetaan CMT tidak ditebak. Roll lama Siap kirim perlu pencatatan kirim pertama
melalui transisi legacy.

## Pemeriksaan

```powershell
./.venv/Scripts/python.exe manage.py check
./.venv/Scripts/python.exe manage.py test tracking
./.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
./.venv/Scripts/python.exe manage.py audit_workflow --json
```

Tes lokal tanpa `TEST_DATABASE_URL` menggunakan database tes SQLite tersendiri.
Tes transaksi bersamaan otomatis dilewati di SQLite karena penguncian baris harus
dibuktikan dengan PostgreSQL. Gunakan PostgreSQL tes terpisah melalui
`TEST_DATABASE_URL` untuk menjalankan dua pengajuan roll bersamaan dan dua kiriman
yang sisa gabungannya tidak cukup. Jangan gunakan database operasional sebagai
database tes. Lihat catatan implementasi untuk batas verifikasi yang telah dijalankan.

Verifikasi terbaru tanggal 5 Oktober 2026: 70 tes, 64 lulus dan 6 tes khusus
PostgreSQL dilewati. Database aktif lokal `severli.sqlite3` sudah dimigrasi hingga
`0007` setelah backup dan audit salinan. Pemeriksaan 18 halaman/ekspor pada
database lokal aktif lulus dengan koneksi read-only; field transaksi lama dan
totalnya cocok dengan backup. Pemisahan bahan/warna juga lulus smoke test database
memori. Penguncian PostgreSQL serta QA visual browser masih perlu diperiksa;
pembaruan bahan/warna belum dideploy ke produksi.

Untuk audit salinan database SQLite lokal yang ada pada workspace ini, gunakan
`./.venv/Scripts/python.exe scripts/audit_local_migrations.py`. Script membaca
database sumber dengan koneksi read-only, menjalankan migrasi hanya pada salinan
di `.verification/`, dan menuliskan laporan konservasi. Database dengan schema
aplikasi lain dicatat sebagai pengecualian tanpa dipaksakan masuk ke model ini.
