# Operasi dan pemulihan

## Pemeriksaan rutin

- Pantau `/healthz/` dari monitoring Render. Endpoint hanya melaporkan status ketersediaan, tanpa informasi koneksi.
- Jalankan `python manage.py verify_ledger` setelah migrasi, koreksi besar, dan pemulihan.
- Jalankan `python manage.py check_integrations` saat konfigurasi database/bucket berubah.
- Selesaikan daftar Selisih & Resolusi, penerimaan gudang tertunda, dan sisa bahan sebelum menutup PO.
- Error HTTP menyertakan header `X-Request-ID`; halaman error menampilkan kode permintaan. Cocokkan waktu dan request ID dengan log Render. Jangan mencatat password, URI database, atau service key dalam tiket.

## Backup database

Atur jadwal backup/PITR pada proyek Supabase sesuai paket dan kebutuhan pengguna. Kapabilitas dan retensi bergantung pada paket; periksa [dokumentasi backup Supabase](https://supabase.com/docs/guides/platform/backups). Kebijakan jadwal dan retensi belum diterapkan pada akun pengguna.

Backup mandiri schema aplikasi memakai `pg_dump` versi yang sesuai server. Simpan URI lewat secret environment terminal, lalu:

```bash
pg_dump --dbname="$DATABASE_URL" --schema=po_tracking --format=custom --no-owner --file=tracking-backup.dump
```

Perintah di atas dijalankan administrator pada mesin backup, bukan sebagai request web. Enkripsi salinan, batasi akses, dan simpan di lokasi yang terpisah dari database. Usulan awal: harian, 30 salinan harian, serta backup sebelum deployment; pemilik proses perlu menetapkan retensi final, RPO, dan RTO.

## Backup lampiran

Backup database Supabase tidak mencakup isi objek Storage. Lampiran perlu salinan terpisah. Gunakan:

```bash
python manage.py backup_evidence --output /lokasi-backup/bukti-YYYY-MM-DD
```

Perintah menyalin bukti yang tercatat, memeriksa SHA256, dan menulis manifest. Jadwalkan di host backup dengan akses ke environment Supabase dan media penyimpanan persisten, bukan disk sementara layanan web. Salinan ini belum dijadwalkan otomatis pada akun pengguna.

## Backup data tanpa pg_dump

Untuk backup data aplikasi yang dapat dibuat langsung melalui Python:

```bash
python manage.py backup_data --output /lokasi-backup/tracking-YYYY-MM-DD.json.gz
```

PostgreSQL memakai snapshot repeatable-read agar data antar-tabel konsisten. Backup berisi data aplikasi, akun, audit, keputusan, dan referensi lampiran; schema dipulihkan dari migration yang disertakan dalam repository. File manifest mencatat migration, waktu, dan SHA256. Simpan kedua file bersama kode versi yang sama. Isi bukti tetap dicadangkan dengan `backup_evidence`.

Pulihkan hanya ke database staging baru yang sudah dimigrasikan memakai versi kode yang sama:

```bash
python manage.py prepare_database
python manage.py migrate --noinput
python manage.py restore_data --input /lokasi-backup/tracking-YYYY-MM-DD.json.gz --confirm-empty-database
```

Restore menolak checksum yang tidak cocok, migration yang berbeda, dan database aplikasi yang sudah berisi data. Setelah restore, pulihkan lampiran dengan storage key yang sama. Snapshot data berisi hash password dan data operasional; perlakukan sebagai backup privat yang dienkripsi.

Alur backup/restore JSON gzip ini sudah diuji pada dua database PostgreSQL lokal terpisah, termasuk kesamaan seluruh jumlah record dan saldo, checksum lampiran, penolakan database nonkosong, dan trigger append-only setelah pemulihan. Uji akun Supabase nyata tetap diperlukan.

Upload ke Storage dan commit PostgreSQL bukan satu transaksi terdistribusi. Jika penyimpanan objek berhasil tetapi transaksi aplikasi gagal, objek bisa tersisa tanpa referensi. Bandingkan daftar objek bucket dengan `Evidence.storage_key`; tinjau objek yang tidak memiliki referensi dan lebih tua dari 24 jam sebelum menghapusnya. Jangan hapus otomatis objek yang masih dapat dipakai request aktif. Draft yang sudah tersimpan dan bukti transaksi valid tetap dipertahankan.

## Uji pemulihan

1. Siapkan proyek/database dan bucket staging terpisah. Jangan menjalankan restore percobaan pada produksi.
2. Pulihkan dump ke database kosong dengan `pg_restore --no-owner --dbname="$STAGING_DATABASE_URL" tracking-backup.dump`.
3. Salin isi backup lampiran ke bucket staging dengan storage key yang sama. Verifikasi seluruh checksum dari manifest.
4. Pasang versi kode yang sesuai backup. Jalankan `prepare_database`, `migrate`, `check_integrations`, dan `verify_ledger`.
5. Cocokkan jumlah penerimaan, PO, movement, audit, dan lampiran. Periksa saldo per lot serta PO 0111 atau sampel operasional yang sudah disetujui.
6. Uji login beberapa role, akses bukti, ekspor, serta satu transaksi baru. Catat durasi pemulihan dan setiap selisih.

Prosedur ini sudah disediakan; pemulihan terhadap backup Supabase/Storage nyata masih memerlukan akses proyek pengguna.

## Rollback deployment

Untuk perubahan aplikasi tanpa perubahan skema yang tidak kompatibel, gunakan rollback ke deployment Render terakhir yang sehat. Untuk perubahan skema, utamakan perbaikan melalui migrasi maju. Jangan menurunkan migration produksi secara otomatis: reversing migrasi proteksi menghilangkan trigger append-only, dan menghapus tabel dapat menghilangkan riwayat.

Jika perlu mengembalikan database dari backup, hentikan penulisan, simpan salinan kondisi terkini, pulihkan di staging terlebih dahulu, dan rekonsiliasi transaksi setelah titik backup. Penggantian database produksi memerlukan keputusan pemilik proses.

## UAT

Purchasing perlu menguji penerimaan multi-baris, draft/revisi, dispatch parsial, laporan CMT, hasil gudang, retur, waste, transfer, reversal, dan ekspor. Approver perlu menguji approve/reject/revision serta kondisi stok berubah sebelum keputusan. Management memeriksa keterbacaan angka dan filter. Super Admin memeriksa pengelolaan akun tanpa memperoleh akses transaksi rutin.

UAT oleh pengguna dan uji beban dengan volume operasional yang disepakati belum dilakukan. Jangan menyatakan performa produksi atau kesiapan operasional penuh hanya berdasarkan pengujian lokal.
