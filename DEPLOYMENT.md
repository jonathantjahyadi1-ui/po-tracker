# Pemasangan Supabase dan Render

Kode sudah siap dipasang. Langkah ini membutuhkan repository Git, proyek Supabase, dan layanan web Render milik pengguna. Belum ada resource eksternal yang dibuat atau deployment yang dilakukan oleh aplikasi lokal ini.

## 1. Supabase PostgreSQL

1. Pilih proyek Supabase untuk aplikasi ini.
2. Buka **Connect → Session pooler**. Gunakan koneksi port 5432. Session pooler cocok untuk backend Django yang berjalan terus-menerus dan dapat diakses lewat IPv4. Lihat [panduan koneksi Supabase](https://supabase.com/docs/guides/database/connecting-to-postgres).
3. Simpan URI sebagai `DATABASE_URL` di environment Render. URL-encode password apabila memiliki karakter khusus. Jangan commit URI ke Git.
4. Gunakan `DATABASE_SSL=require`. Untuk verifikasi sertifikat penuh, pasang CA Supabase dan gunakan `sslmode=verify-full` dengan `sslrootcert` pada URI; ikuti [pengaturan SSL Supabase](https://supabase.com/docs/guides/platform/ssl-enforcement).

Contoh struktur URI, bukan kredensial nyata:

```text
postgresql://postgres.PROJECT:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
```

Perintah pre-deploy membuat schema `po_tracking`, lalu menjalankan migrasi. Role koneksi awal harus memiliki izin membuat schema/tabel. Semua tabel aplikasi tetap di schema ini. Jangan menambahkan `po_tracking` ke schema Data API yang diekspos. Skema `public` Supabase yang sudah ada tidak diganti/dihapus.

Untuk pemisahan izin lebih ketat, gunakan role pemilik schema khusus aplikasi untuk migrasi dan role runtime dengan policy/grant yang ditetapkan DBA. Jangan mengganti ke role nonowner hanya dengan GRANT tabel: RLS juga harus memiliki policy yang sesuai, atau akses runtime akan ditolak.

## 2. Supabase Storage

1. Buat bucket bernama `po-evidence`, dengan **Public bucket = off**.
2. Tetapkan batas file 10 MB serta tipe `application/pdf`, `image/jpeg`, `image/png`.
3. Isi `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, dan `SUPABASE_STORAGE_BUCKET` pada environment Render. Service role key hanya dibaca backend dan tidak dikirim ke browser.
4. Lampiran dibuka lewat route aplikasi yang memeriksa login, lalu diunduh sebagai attachment. Tidak ada URL file publik.

Lihat [pengaturan bucket Supabase](https://supabase.com/docs/guides/storage/buckets/creating-buckets). Jika upload gagal, posting ditolak dan halaman menampilkan data form agar dapat diperbaiki/dikirim ulang. Upload berhasil yang kemudian gagal commit database dapat meninggalkan objek tanpa referensi; rekonsiliasi objek dibahas dalam panduan operasi.

## 3. Repository dan layanan Render

1. Masukkan isi folder `po-tracker` ke root repository Git privat. Jangan sertakan `.env`, database lokal, folder upload lokal, atau virtual environment.
2. Di Render, pilih **New → Blueprint**, lalu hubungkan repository tersebut.
3. Periksa nama layanan dan paket sebelum menerapkan Blueprint. `render.yaml` mengusulkan region Singapore dan paket `0.5c-512mb`. Sesuaikan dengan paket yang disetujui pengguna. Tidak ada database Render yang dibuat karena database berada di Supabase.
4. Isi environment yang diminta Blueprint. `SECRET_KEY` dibuat Render. `DEBUG=false` dan aturan bisnis sudah dikonfirmasi.
5. Jalankan deployment. Build memasang versi dependency terkunci, mengumpulkan static files, dan memeriksa konfigurasi produksi. Pre-deploy menyiapkan schema dan migrasi; start menjalankan Gunicorn.
6. Render menyediakan hostname melalui `RENDER_EXTERNAL_HOSTNAME`; aplikasi menambahkannya ke allowed hosts dan trusted origins. Untuk domain kustom, tambahkan `ALLOWED_HOSTS` dan `CSRF_TRUSTED_ORIGINS=https://domain-anda`.

Blueprint mengikuti [referensi Render](https://render.com/docs/blueprint-spec); alur Django mengikuti [panduan deployment Django](https://render.com/docs/deploy-django). Auto-deploy dinonaktifkan agar perubahan tidak terpasang sebelum diperiksa. Deployment awal ini tetap memerlukan verifikasi di akun Render pengguna.

## 4. Akun awal dan verifikasi

Jalankan di Render Shell:

```bash
python manage.py check_integrations
python manage.py create_admin --username administrator --email nama@perusahaan.com
python manage.py verify_ledger
```

Password admin dimasukkan interaktif dan tidak dicetak. Setelah masuk, buat akun Purchasing, Approver, dan Management melalui **Akun pengguna**. Berikan hak adjustment/reopen hanya pada Purchasing yang ditunjuk.

Lakukan transaksi uji kecil di lingkungan staging: terima bahan, approve alokasi, kirim parsial, terima CMT, catat hasil gudang, ekspor, dan unduh bukti. Uji langsung bahwa akun Management/Super Admin tidak bisa posting transaksi. Setelah UAT disetujui, gunakan database produksi kosong atau prosedur saldo awal beralasan; jangan menyalin data demo.

## Data konfigurasi yang diperlukan

| Environment | Fungsi |
|---|---|
| `DATABASE_URL` | URI PostgreSQL Supabase Session pooler |
| `DATABASE_SSL` | `require` di produksi |
| `SECRET_KEY` | Secret acak Django |
| `SUPABASE_URL` | URL proyek Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | Key backend untuk bucket privat |
| `SUPABASE_STORAGE_BUCKET` | `po-evidence` |
| `DEBUG` | `false` di produksi |
| `BUSINESS_RULES_CONFIRMED` | `true`, sesuai konfirmasi pengguna |
| `ALLOWED_HOSTS` | Tambahan domain kustom, dipisahkan koma |
| `CSRF_TRUSTED_ORIGINS` | Tambahan origin HTTPS, dipisahkan koma |

Simpan kredensial langsung pada environment Render atau `.env` lokal yang diabaikan Git. Tidak perlu menempelkan password/database key di percakapan.
