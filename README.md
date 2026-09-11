# Rantai

Sistem tracking bahan dan PO produksi berdasarkan PRD versi 1.0, 10 September 2026. Bahasa antarmuka Indonesia, zona waktu Asia/Jakarta, warna putih/arang/hijau tanpa gradasi.

Aplikasi sudah dapat dijalankan secara lokal. Koneksi ke proyek Supabase, bucket lampiran, dan deployment pada akun Render pengguna belum dilakukan karena detail akun/repository belum diberikan. Data demo bukan data operasional.

## Menjalankan di Windows

Pasang Python 3.12, kemudian buka PowerShell di folder aplikasi:

```powershell
./run-local.ps1
```

Buka `http://127.0.0.1:8765/`. Pilih role pada bagian **Pratinjau data contoh**, lalu **Buka demo**. Akun contoh memakai kata sandi yang tidak bisa dipakai login; akses demo hanya berlaku pada localhost, DEBUG, dan SQLite. Fitur demo tidak tersedia saat memakai Supabase atau mode produksi.

Alternatif manual:

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.lock.txt
$env:DEBUG='true'
./.venv/Scripts/python.exe manage.py migrate
./.venv/Scripts/python.exe manage.py seed_demo
./.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8765
```

Untuk mode operasional, gunakan `.env.example` sebagai acuan dan ikuti [DEPLOYMENT.md](DEPLOYMENT.md). Jangan memakai SQLite untuk pengguna bersamaan atau menyimpan lampiran operasional pada disk sementara Render.

## Alur penggunaan

1. Purchasing melengkapi Vendor, CMT, Bahan, Warna, Satuan, Produk, Target, dan Gudang pada Data Master.
2. Purchasing membuat penerimaan dengan invoice dan beberapa baris bahan. Simpan draft, periksa detail, lalu posting. Setiap baris membentuk lot dan stok fisik.
3. Buat dan aktifkan PO. Tambahkan CMT pelaksana, target, dan tanggal selesai.
4. Buat alokasi dari lot/lokasi, kemudian ajukan. Approver masuk dengan akun terpisah untuk menyetujui, menolak, atau meminta revisi.
5. Purchasing membuka alokasi disetujui, memilih Kirim Bahan, menyimpan draft surat jalan, lalu posting pengiriman. Pengiriman parsial diperbolehkan.
6. Buka detail pengiriman dan catat penerimaan setiap baris lot berdasarkan laporan CMT. Sisa yang belum diterima tetap dalam perjalanan. Catat selisih secara eksplisit.
7. Catat progres dan pengiriman hasil dari PO. Purchasing mencatat penerimaan gudang, memisahkan good dan reject.
8. Rekonsiliasi sisa di CMT menjadi terpakai, retur, waste, rusak, atau transfer. Transfer harus melalui alokasi transfer baru yang disetujui.
9. Selesaikan selisih dan kelebihan hasil. Tutup PO setelah seluruh syarat terpenuhi. Arsip tersedia setelah PO ditutup/dibatalkan.
10. Pilih laporan dan filter. Ekspor Excel memakai filter dan urutan yang sama dengan daftar.

## Role

| Tindakan | Purchasing | Approver | Management | Super Admin |
|---|---|---|---|---|
| Lihat transaksi dan dashboard | Ya | Ya | Ya | Ya |
| Input master dan transaksi | Ya | Tidak | Tidak | Tidak |
| Setujui/tolak/revisi alokasi | Tidak | Ya | Tidak | Tidak |
| Adjustment / reopen | Dengan hak khusus | Tidak | Tidak | Tidak |
| Ekspor | Ya | Tidak | Ya | Ya |
| Kelola akun dan role | Tidak | Tidak | Tidak | Ya |
| Audit | Aktivitas sendiri | Aktivitas sendiri | Tidak | Seluruh audit |

CMT dan Gudang adalah data referensi, tanpa akun aplikasi. Super Admin tidak mendapat akses operasional otomatis. Semua aturan diperiksa ulang di server, termasuk saat endpoint dipanggil langsung.

## Keputusan bisnis

Default berikut disetujui pengguna pada 11 September 2026:

- PO berarti Production Order.
- Yard menggunakan 2 desimal; roll bilangan bulat. Pelacakan dilakukan per lot dan jumlah roll agregat.
- Satu PO dapat memakai beberapa CMT, invoice, dan lot. Sistem mendukung beberapa gudang.
- Good yang diterima gudang menentukan remaining. Reject dicatat terpisah.
- Penutupan memerlukan target good terpenuhi, tidak ada reservasi/perjalanan/bahan CMT tersisa, seluruh pengiriman hasil selesai, dan seluruh selisih terselesaikan.
- Kelebihan hasil tetap terlihat dan memerlukan alasan penyelesaian manual sebelum close. Penerimaan baru membatalkan persetujuan kelebihan sebelumnya.
- Bukti wajib untuk selisih, waste, kerusakan, reversal, serta adjustment. Format PDF/JPG/PNG maksimal 10 MB.
- Standar pemakaian disimpan sebagai informasi; target PO tidak dihitung otomatis dari angka 1,65 yard.

Aturan implementasi yang dibuat eksplisit:

- Setiap penerimaan gudang harus mengklasifikasikan semua pcs: `received = good + reject`.
- Penerimaan nol diperbolehkan hanya sebagai laporan selisih dengan bukti, misalnya seluruh kiriman hilang. Resolusi kehilangan tidak menambah good.
- Permintaan transfer bahan keluar yang masih draft/pending/revisi harus diselesaikan atau dibatalkan sebelum PO sumber ditutup.
- Invoice revisi memakai nomor unik dan menautkan invoice lama yang sudah direversal.
- Filter PO pada laporan stok berarti **lot terkait PO**, sehingga saldo fisik lot bersama tidak berubah menjadi negatif akibat menghilangkan penerimaan asalnya. Laporan alokasi/CMT/PO menampilkan kuantitas yang khusus dimiliki PO.
- Saldo stok adalah kumulatif sampai tanggal akhir. Tanggal awal berlaku untuk laporan pergerakan, bukan menghapus saldo awal.
- Resolusi kehilangan membutuhkan Purchasing dengan hak adjustment. Sisa kehilangan hasil menggunakan field pcs, bukan yard.
- Draft belum boleh digunakan transaksi lanjutan. Pengeditan draft memakai pemeriksaan versi untuk mencegah perubahan saling menimpa.
- Batas ekspor sinkron 50.000 baris. Persempit filter bila lebih besar; pemrosesan ekspor asinkron belum disediakan.
- Retensi data otomatis dan impor Excel lama belum diaktifkan. Tidak ada data transaksi yang dihapus otomatis.

## Struktur teknis

```text
config/                 Konfigurasi Django, route, WSGI
tracking/models.py      Model data dan constraint
tracking/services.py    Transaksi bisnis, locking, ledger, status
tracking/forms.py       Kontrak form dan validasi
tracking/views.py       Endpoint, akses role, laporan, ekspor
tracking/reports.py      Filter dan penyajian data laporan
tracking/storage.py     Validasi dan penyimpanan bukti privat
tracking/migrations/    Migrasi skema dan proteksi append-only
tracking/tests.py       Acceptance, akses, koreksi, dan concurrency
templates/              Halaman aplikasi
static/                 CSS, JavaScript, ikon
render.yaml             Blueprint Render
```

Django menangani login/password/session; Supabase dipakai untuk PostgreSQL dan Storage. Aplikasi mengakses database melalui backend, bukan Supabase Data API dari browser. Skema `po_tracking` tidak diekspos publik. Stock movements, audit, dan keputusan approval dilindungi trigger database. Master memakai tabel bertipe (`kind`) dengan constraint kode per jenis; setiap transaksi memiliki FK bertipe yang divalidasi oleh service.

Konfirmasi stok dan pembaruan status berada dalam satu transaksi database. Urutan lock: PO, header, lalu lot yang diurutkan berdasarkan ID. Yard dan nominal menggunakan Decimal di aplikasi dan NUMERIC di PostgreSQL. Tidak ada input saldo bebas-edit.

## Pengujian

```powershell
./.venv/Scripts/python.exe manage.py test tracking --verbosity 2
./.venv/Scripts/python.exe manage.py verify_ledger
./.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
```

Jalankan suite concurrency pada PostgreSQL terpisah, bukan database operasional. Django membuat dan menghapus database pengujian. Rincian hasil dan batas verifikasi terdapat di [VALIDATION.md](VALIDATION.md). CI PostgreSQL tersedia di `.github/workflows/test.yml`.

Sebelum dipakai operasional, lakukan UAT Purchasing/Approver, uji bucket Supabase nyata, uji backup/restore ke lingkungan terpisah, dan sepakati volume target serta retensi. [OPERATIONS.md](OPERATIONS.md) memuat langkah operasional.
