# Revisi alur dan UI/UX — catatan implementasi

Sumber kebutuhan: PRD versi 1.0 tanggal 5 Oktober 2026. K adalah permintaan
pengguna, E adalah perilaku existing yang dipertahankan, dan U adalah detail
default yang masih berupa usulan. Catatan ini mendokumentasikan revisi kode;
tidak menyatakan bahwa data atau layanan produksi sudah dimigrasi/dideploy.

## Pilihan default U

| ID | Default yang digunakan | Status keputusan |
|---|---|---|
| U-01 | Nama produk opsional di CMT saat penautan PO; dapat diubah melalui CMT. Form Informasi PO hanya tanggal order. | Usulan implementasi, belum disahkan pengguna. |
| U-02 | Tanggal kirim daftar PO adalah tanggal kiriman gudang terakhir. Tanggal kirim kain terpisah di CMT. | Usulan implementasi, belum disahkan pengguna. |
| U-03 | No. pengiriman menggunakan primary key alokasi yang stabil, tanpa nomor PO sementara. | Usulan implementasi, belum disahkan pengguna. |
| U-04 | Pengajuan boleh mengambil beberapa bahan/warna dari satu invoice untuk satu CMT. | Usulan implementasi, belum disahkan pengguna. |
| U-05 | Satu PO memiliki satu CMT, nomor PO unik global; beberapa alokasi CMT yang sama boleh menautkan PO yang sama tanpa menimpa produk. | Usulan implementasi, belum disahkan pengguna. |
| U-06 | Penerimaan boleh sebagian; penautan PO menunggu seluruh roll alokasi Diterima. | Usulan implementasi, belum disahkan pengguna. |
| U-07 | Status Belum ada hasil, Belum lengkap, Belum ada hasil produksi, dan Lebih kirim menjelaskan data; semua hasil nol tidak Done. | Usulan implementasi, belum disahkan pengguna. |
| U-08 | Tolak kiriman melebihi sisa warna serta hasil di bawah kiriman tercatat. Anomali lama dipertahankan. | Usulan implementasi, belum disahkan pengguna. |
| U-09 | Done dihitung otomatis dan dapat kembali Kurang kirim setelah hasil diperbesar. | Usulan implementasi, belum disahkan pengguna. |

## Model dan kompatibilitas

- Invoice memiliki `InvoiceMaterial` dan `InvoiceColor`; masing-masing roll
  terhubung ke baris bahan/warna dan tetap menyimpan bahan, warna, panjang serta
  identitasnya sendiri. Hubungan `InvoicePo` lama tetap dipertahankan untuk sejarah.
- Alokasi boleh belum memiliki PO. Pengajuan dan ACC tidak menciptakan PO.
  ACC mencatat tanggal kirim dan mengubah seluruh roll ke Dikirim dalam transaksi.
- PO baru memiliki CMT. PO lama dipetakan hanya jika alokasi historis memiliki
  tepat satu CMT. PO tanpa CMT atau lintas CMT tetap menjadi pengecualian yang
  dapat dibuka; tidak dibuat kiriman, CMT, tanggal, produk, atau PO fiktif.
- Siap kirim dipertahankan sebagai keadaan legacy. Pengiriman pertamanya dicatat
  secara eksplisit. Terpakai/Rusak dan catatan penutupan lama tetap tersimpan.
- Catatan invoice serta Produk/Pemakaian std/Catatan PO historis tetap tersimpan
  ketika field tidak dikirim oleh formulir baru. Lampiran tetap di database.
- Hasil kosong tidak dianggap nol. Resolver yang sama menghitung sisa dan
  kelebihan per warna untuk daftar, detail, ringkasan, filter, serta ekspor.
- Identitas permintaan kiriman mencegah pengulangan POST yang sama menjadi
  transaksi ganda. Permintaan baru yang isinya identik tetap menjadi transaksi sah
  jika hasil serta sisa mengizinkannya.
- Ledger `InvoiceWrite` pada migrasi `0006_invoice_write_requests` menyimpan
  identitas permintaan penyimpanan invoice. POST edit yang diulang tidak menambah
  roll baru atau log simpan ganda; permintaan baru dengan isi yang sama tetap
  dapat menambah roll sebagai transaksi terpisah. Ledger juga memeriksa pengguna,
  invoice tujuan, metadata, panel bahan/warna, perubahan roll existing, dan lampiran.

## Cakupan tes otomatis

Tes mempertahankan cakupan parser angka Indonesia, tempel/import CSV/XLSX,
normalisasi master/PO, collision alokasi, aturan tanggal, metadata terkunci,
lampiran/unduh/penggantian, ekspor, hak akses, dan anggaran query. Assertion lama
yang mengharuskan PO pada invoice/alokasi, Siap kirim setelah ACC, atau tombol
penyelesaian manual disesuaikan dengan alur PRD. Pengiriman pertama dari Siap kirim
legacy tetap diuji; Terpakai/Rusak lama dipertahankan sebagai data historis tanpa
menghidupkan kembali tombol kontrol produksi yang dihapus.

Tambahan penerimaan mencakup invoice 2 bahan/3 warna/7 roll tanpa PO, kegagalan
baris atomik dan input dipertahankan, pencarian vendor sebelum pagination,
alur HTTP tanpa PO sampai penerimaan sebagian dan penautan PO, penautan PO CMT
yang sama/berbeda, ACC ulang, status 200 → 150 → tambah 50 → Done → hasil 230,
hasil kosong/nol dan anomali yang tidak saling meniadakan antarwarna, validasi
kelebihan/koreksi hasil, permintaan ulang kiriman, CSRF/POST, serta konsistensi ekspor.

Regresi navigasi CMT diuji dengan 21 pengiriman: tautan langsung pengiriman lama
membuka halaman pagination yang tepat; POST penerimaan/penautan PO mempertahankan
konteks pengiriman dan anchor. ID pengiriman yang tidak valid menampilkan kesalahan
tanpa mengubah PO, log atau status roll.

Tes PostgreSQL khusus memeriksa dua pengajuan roll yang sama dan dua kiriman
bersamaan yang melebihi sisa gabungan. SQLite tidak membuktikan perilaku
penguncian; tes tersebut dilewati jika PostgreSQL tes tidak tersedia.

## Verifikasi data dan batas rilis

Script `scripts/audit_local_migrations.py` membuat salinan konsisten melalui
koneksi SQLite read-only dari database lokal yang ada, kemudian menjalankan
migrasi pada salinan di `.verification/`. Prosedur audit salinan ini tidak
memodifikasi database sumber. Laporan
membandingkan jumlah invoice/roll/alokasi/PO/hasil/kiriman/lampiran/log, total
yard/pcs, serta digest semua field lama termasuk lampiran biner dan hubungan
historis. Isi kredensial atau data akun tidak dicetak.

### Hasil verifikasi lokal, 5 Oktober 2026

- Pemeriksaan Django, pemeriksaan perubahan migrasi, lint Python, dan sintaks
  JavaScript lulus. Suite integrasi akhir yang mencakup model, service, view,
  template, ekspor, replay permintaan dan migrasi: **57 tes dalam 6,685 detik;
  52 lulus, 5 dilewati** karena khusus PostgreSQL.
- Fixture migrasi `0003 → 0006` mempertahankan seluruh field lama pada invoice,
  grup PO legacy, PO, alokasi, roll, lampiran biner, hasil, kiriman dan log.
  Fixture memeriksa bahwa semua nilainya tetap identik.
  Fixture tersebut mencakup override bahan/warna, Siap kirim/Terpakai/Rusak lama, PO tanpa CMT,
  PO lintas CMT, hasil nol, serta kiriman lama melebihi hasil. Menjalankan pemetaan
  histori ulang tidak menambah panel bahan atau membership alokasi ganda.
- Salinan `severli.sqlite3` berhasil dimigrasi. Jumlah sebelum/sesudah sama:
  1 invoice, 15 roll, 1 alokasi, 1 PO, 1 hasil, 4 kiriman, 0 lampiran, dan 78 log.
  Total sebelum/sesudah: **1773 yard, 1072 hasil pcs, 1072 kiriman pcs**. Digest
  semua field transaksi lama identik; CMT dipetakan deterministik. Hash sumber
  tetap sama selama audit salinan. Rincian berada pada
  `.verification/migration-conservation.json` sebagai artefak audit lokal;
  ringkasan jumlah dan total di atas merupakan catatan yang disimpan di proyek.
- `local.sqlite3` berasal dari versi aplikasi lain (`tracking_order`,
  `tracking_lot`, dan relasi terkait), tanpa tabel invoice/roll/alokasi/PO versi
  ini. Migrasi revisi ini tidak cocok untuk schema tersebut; database itu tidak
  dimodifikasi dan tidak dinyatakan lulus migrasi.
- Database aktif lokal `severli.sqlite3` kemudian dimigrasi sampai `0006` setelah
  backup konsisten melalui koneksi read-only ke
  `.verification/severli-before-prd-20261005.sqlite3` (artefak lokal privat).
  Audit sesudah aktivasi mempertahankan jumlah transaksi, total yard/pcs, serta
  78 log yang sama. Struktur baru memiliki 1 panel bahan, 1 baris warna,
  15 membership alokasi, dan 0 ledger `InvoiceWrite`. PO dipetakan ke CMT yang
  diketahui, berstatus Done, tanpa pengecualian audit. `local.sqlite3` tetap utuh.
- Perbandingan seluruh field transaksi lama pada database lokal yang diaktifkan
  dengan backup menunjukkan digest dan total yang identik. Smoke test 18 halaman
  dan ekspor pada data lokal nyata lulus melalui koneksi SQLite dengan
  `PRAGMA query_only=ON`, sehingga pemeriksaan halaman tidak menulis transaksi.
- Rendering Django memverifikasi form bahan/warna, field yang dihapus, menu CMT
  → PO, kondisi kosong, dan status yang konsisten. Pemeriksaan visual browser
  pada 360/768/1366/1920 px, interaksi keyboard/drawer, serta reduced motion
  **belum diverifikasi secara langsung** karena browser otomatis tidak tersedia.
- PostgreSQL tes tidak tersedia secara lokal. Tes penguncian alokasi, kiriman
  bersamaan, dan perubahan CMT bersamaan disiapkan, tetapi **belum dijalankan**.
  SQLite membuktikan logika bisnis dan migrasi sampel; tidak membuktikan
  semantik penguncian PostgreSQL.

Aktivasi database lokal di atas merupakan langkah terpisah dari audit salinan.
Tidak ada migrasi database PostgreSQL/Supabase atau deployment layanan produksi
yang dijalankan dalam pekerjaan ini.
Sebelum produksi, ulangi audit salinan PostgreSQL dari schema aktif, selesaikan
pengecualian mapping yang relevan, dan jalankan tes transaksi PostgreSQL serta
QA visual. Database lokal bukan bukti kondisi server.
