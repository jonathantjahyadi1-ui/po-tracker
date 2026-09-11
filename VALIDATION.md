# Hasil verifikasi

Tanggal: 11 September 2026. Lingkungan: Windows, Python 3.12.14, Django 5.2.17, PostgreSQL 18.4 lokal. Pengujian tidak memakai database atau akun Supabase produksi.

## Pengujian otomatis

Suite final **48 pengujian lulus** dalam 36,541 detik di PostgreSQL, tanpa skip. Kode seluruh pengujian disertakan pada `tracking/tests.py`.

| PRD | Perilaku yang diverifikasi | Hasil |
|---|---|---|
| AT-01 | Posting 50 roll / 1.665,50 yard tepat sekali | Lulus |
| AT-02 | Invoice vendor duplikat setelah normalisasi | Ditolak |
| AT-03 | Alokasi melebihi stok bebas | Ditolak |
| AT-04 | Dua approval bersamaan atas stok terakhir | Hanya satu berhasil |
| AT-05 | Approval menaikkan reservasi, fisik tetap | Lulus |
| AT-06 | Dispatch parsial mengurangi fisik/reservasi dan menambah transit | Lulus |
| AT-07 | Penerimaan CMT parsial menyisakan transit | Lulus |
| AT-08 | Selisih membutuhkan bukti dan resolusi | Lulus |
| AT-09 | Good 430 dan reject 10 dihitung terpisah | Lulus |
| AT-10 | 430 + 247 + 250 + 145 = 1.072; penutupan setelah rekonsiliasi | Lulus |
| AT-11 | Kelebihan hasil tetap terlihat dan close membutuhkan resolusi | Lulus |
| AT-12 | Edit langsung transaksi posted | Ditolak |
| AT-13 | Management/Approver/Admin mencoba posting melalui endpoint | HTTP 403; tidak ada perubahan bisnis |
| AT-14 | Ekspor mengikuti periode/CMT dan data aktif | Lulus |
| AT-15 | Penutupan dengan bahan unresolved | Ditolak |
| AT-16 | Posting bersamaan dan pengiriman ulang token sama | Satu transaksi; tidak ada posting ganda |

Pengujian tambahan mencakup reversal penerimaan/pengiriman/CMT/gudang, retur, waste, transfer dengan alokasi baru, hak adjustment, reopen beralasan, revisi dengan versi, master nonaktif, duplikasi nomor PO dengan variasi separator, validasi desimal, CSRF, pembatasan akun, invalid upload, trigger append-only, seluruh route baca per role, form penerimaan multi-baris, pagination, saldo awal kartu stok, lot bersama pada filter PO, seluruh kiriman hasil hilang, serta penutupan PO dengan permintaan transfer keluar yang belum selesai.

## Pemeriksaan aplikasi dan deployment

- Migrasi pada database kosong dan trigger append-only: berhasil di SQLite lokal serta PostgreSQL.
- `makemigrations --check --dry-run`: tidak ada perubahan model yang belum dimigrasikan.
- `check --deploy --fail-level WARNING`: lulus tanpa masalah.
- Static files terkumpul dengan manifest dan kompresi.
- `render.yaml` tervalidasi terhadap JSON Schema resmi Render.
- Pratinjau lokal `/` dan `/login/`: HTTP 200 setelah redirect login yang sesuai.
- Halaman dan hak akses diuji dengan Django HTTP client. Pemeriksaan visual/klik otomatis di browser belum dilakukan.

## Pengukuran lokal

Data sintetis: 1.000 PO, pagination 25 baris, PostgreSQL lokal. Data pengukuran di-rollback setelah selesai.

| Permintaan | Waktu server | Query |
|---|---:|---:|
| Daftar PO halaman 1 | 208,2 ms | 32 |
| Daftar PO halaman 20 | 147,8 ms | 32 |
| Detail PO | 90,0 ms | 14 |

Angka di atas tidak memasukkan jaringan Render–Supabase atau rendering browser, dan bukan hasil uji beban pengguna bersamaan. Detail PO yang diuji hanya memuat data dasar; performa timeline dengan riwayat produksi besar perlu diuji pada volume yang disepakati.

## Uji pemulihan lokal

Backup JSON gzip dan lampiran dipulihkan ke database PostgreSQL baru dengan migration yang sama. Seluruh jumlah record pada 24 model aplikasi cocok, termasuk 26 audit, 9 pergerakan, 1 PO, 1 invoice, 1 keputusan approval, dan 1 lampiran. Saldo fisik tetap 42 roll / 1.425,50 yard; bahan di CMT tetap 8 roll / 240,00 yard; reservasi dan transit nol; good tetap 430 pcs.

Checksum lampiran cocok. Restore kedua ke database yang sudah terisi ditolak. Percobaan mengubah stock movement setelah restore ditolak oleh trigger database. Pemulihan ini memakai data sintetis dan file bukti lokal, bukan proyek Supabase pengguna.

## Yang belum diverifikasi pada akun pengguna

- Koneksi PostgreSQL Supabase nyata dan hak akses role database.
- Upload/unduh, batas bucket, dan backup lampiran Supabase Storage nyata.
- Deployment dan health check dari layanan Render pengguna.
- Backup terjadwal, restore ke staging, retensi, RPO, dan RTO.
- UAT serta persetujuan Purchasing dan Approver.
- Beban operasional besar dan ekspor di atas 50.000 baris.

Source code dan konfigurasi siap diserahkan, tetapi status operasional produksi menunggu koneksi layanan dan verifikasi tersebut.
