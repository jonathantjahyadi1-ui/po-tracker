# Severli PO Tracker

Pelacakan roll kain dari invoice vendor sampai PO balance. Antarmuka berbahasa Indonesia.

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

Di halaman Tambah bahan, satu invoice dapat memuat hingga 10 grup PO. Nomor PO boleh
diisi saat input atau saat alokasi. Setiap roll memiliki yard sendiri; tombol
"Bedakan detail tiap roll" membuka bahan, warna, dan lokasi khusus per roll.
File invoice PDF, JPG, atau PNG (maksimal 10 MB) disimpan di database dan dapat
diunduh dari halaman detail invoice.

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

`build.sh` membuat schema baru, menjalankan migrasi, lalu menyalin akun lama jika
`LEGACY_USERS_SCHEMA` diisi. Untuk layanan Render yang sudah ada, atur
`DATABASE_SCHEMA=severli` dan `LEGACY_USERS_SCHEMA=po_tracking` sebelum deploy;
schema `po_tracking` tetap tersimpan. Untuk database baru, kosongkan variabel legacy
dan buat admin dengan `python manage.py createsuperuser`.
Data operasional lama tidak dipindahkan dan tetap tersedia di schema `po_tracking`.

## Pemeriksaan

```powershell
./.venv/Scripts/python.exe manage.py check
./.venv/Scripts/python.exe manage.py test tracking
./.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
```
