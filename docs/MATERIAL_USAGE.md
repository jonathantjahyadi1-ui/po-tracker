# Pemakaian Paten Bahan dan Selisih Pengiriman

Implementasi PRD 12 Oktober 2026. Payment tetap memakai alur dan layanan yang sudah ada.

## Penggunaan

1. Buat produk di **Master → Produk** dan bahan di **Master → Bahan**.
2. Buka **Master → Pemakaian Paten Bahan**, pilih produk, lalu isi nilai setiap bahan. Input `2,5` dan `2.5` sama-sama berarti 2,5 yard/pcs. Gunakan **Tambah Bahan** untuk baris tambahan. Nonaktifkan patokan lewat kolom Aktif.
3. Setelah seluruh bahan diterima di CMT, pilih produk dari Master ketika menghubungkan pengiriman bahan ke nomor PO. Preview menampilkan patokan dan estimasi per bahan/warna. Backend memvalidasi ulang dan menyimpan snapshot; patokan yang belum tersedia memblokir penyimpanan hubungan produksi tersebut. Invoice dan reservasi bahan ke CMT tetap dapat dibuat.
4. Di Detail PO, catat hasil aktual per ukuran dan kiriman gudang melalui form yang sudah ada. Bagian **Estimasi dan kelanjutan pengiriman** berada dekat form/riwayat kiriman, dengan yard, snapshot, estimasi, terkirim, dan selisih setiap unit.
5. Pilih **Masih ada kiriman menyusul** untuk proses terbuka atau **Pengiriman sudah final** untuk menutup unit. Kekurangan saat final memerlukan Keterangan Selisih. Catatan saja tidak menutup proses. Setiap unit harus final sebelum PO selesai.

PO aktif lama tanpa snapshot menampilkan **Patokan belum ditetapkan**. Purchasing memilih produk dan menekan **Tetapkan patokan unit lama** setelah mengisi Master. Hanya snapshot yang belum ada yang ditambahkan; snapshot lama tidak disegarkan. PO historis yang sudah selesai tidak dibuka kembali.

## Data dan perhitungan

- `Master.kind=product` menyediakan identitas produk; `Po.product` menyimpan ID. Teks `Po.produk` serta `pemakaian_std` lama tetap dipertahankan.
- `MaterialUsage` unik per produk/bahan, menyimpan angka desimal, status aktif, pembuat, dan pengubah terakhir. Perubahan dicatat di `Log` dengan nilai sebelum/sesudah. Tidak ada penghapusan patokan melalui form.
- `ProductionUnit` unik per PO/bahan/warna mengikuti pemisahan produksi yang sudah ada. Snapshot mencatat produk, sumber Master, angka patokan, waktu, dan aturan HALF_UP. Rincian ukuran tetap menjadi bagian hasil produksi aktual, bukan patokan baru per ukuran.
- Yard adalah jumlah roll yang benar-benar terhubung ke unit tersebut, bukan seluruh invoice. Estimasi dihitung sekali dari total yard memakai Decimal dan pembandingan sisa yang tepat terhadap batas 0,5. Roll menyimpan maksimal 6 angka pecahan; patokan maksimal 8. Input dengan ketelitian berlebih ditolak, bukan dibulatkan diam-diam.
- Parser yard mempertahankan format lama `1.773` sebagai 1.773 yard. Untuk tiga angka desimal yard gunakan koma, misalnya `100,125`. Input patokan memakai format desimal tanpa pemisah ribuan.
- Hasil aktual dan pemakaian aktual tetap terpisah dari estimasi. Kiriman boleh melebihi estimasi; batas terhadap hasil aktual per ukuran yang sudah ada tetap berlaku. Satu bahan adalah acuan satu unit; perhitungan gabungan komponen utama/furing tidak ditambahkan.
- Status daftar, dashboard, Detail PO, dan ekspor menggunakan resolver yang sama. Kelebihan satu unit tidak menghapus kekurangan unit lain. Ekspor menambahkan snapshot, estimasi, selisih, kelanjutan, alasan, dan pelaku/waktu final.

## Konsistensi dan perubahan

Penulisan produksi dan finalisasi mengambil lock PO yang sama sebelum membaca total kiriman. SQLite memakai write lock sebagai pengganti SELECT FOR UPDATE; konflik dikembalikan sebagai respons yang dapat dicoba ulang. ID permintaan kiriman mempertahankan pencegahan submit ganda, termasuk retry setelah final. Nilai snapshot, estimasi, total, dan status tidak diambil dari klien.

Produk draft dapat diganti secara eksplisit sebelum ada kiriman; snapshot baru dan perubahan lama/baru dicatat. Tambahan yard pada unit setelah ada kiriman atau final diblokir melalui alokasi biasa. Jika patokan berubah, alokasi dengan patokan berbeda tidak dicampur pada unit PO yang sama; gunakan PO baru. Tidak ada kewenangan atau form koreksi/pembatalan kiriman gudang baru. Aplikasi sebelumnya belum menyediakan pembatalan kiriman gudang, sehingga AC-27 mengikuti ketersediaan alur koreksi tersebut.

Data historis selesai dapat dilengkapi rincian ukuran/rekonsiliasi yang mempertahankan total melalui alur lama yang tercatat. Perubahan total dan kiriman baru pada PO tersebut tetap diblokir.

## Migrasi dan validasi

Jalankan pada setiap database deployment:

```powershell
.\.venv\Scripts\python.exe manage.py migrate --noinput
```

- `0010_material_usage_snapshots`: menambah struktur patokan/snapshot/finalisasi, identitas produk, penanda histori, dan ketelitian yard.
- `0011_preserve_completed_production`: membuat identitas Master Produk dari nama produk historis dan mempertahankan status selesai berdasarkan data lama, termasuk pemeriksaan ukuran. Tidak menebak patokan atau membuat snapshot dari hasil aktual.

Migrasi lokal `severli.sqlite3` sudah diterapkan setelah backup di `.verification/severli-before-patent-20261012-140533.sqlite3`. Jumlah PO, invoice, roll, hasil, kiriman, pembayaran, dan akun sebelum/sesudah sama.

Pengujian utama terdapat di `tracking/test_material_usage.py`: kombinasi Master/duplikat, desimal koma, seluruh batas HALF_UP pada PRD, penjumlahan roll, pembagian invoice, snapshot, catatan/finalisasi, hasil nol, kelebihan, agregasi unit, hak akses, pemalsuan data klien, idempotensi, migrasi historis, dan konflik penulisan SQLite. Pengujian lama disesuaikan dengan pilihan produk Master dan finalisasi eksplisit. Tes khusus PostgreSQL memerlukan `TEST_DATABASE_URL` PostgreSQL; pengujian lokal memakai SQLite.

Hasil: suite lengkap `python manage.py test tracking --noinput` menjalankan 172 tes, dengan 164 lulus dan 8 dilewati. Pengujian terfokus 37 tes juga lulus. Pemeriksaan Django, `makemigrations --check --dry-run`, lint Python, dan sintaks ketiga file JavaScript lulus. Smoke test HTTP tambahan memeriksa konflik Master (409), validasi angka nol (400), input koma, dan snapshot tetap setelah perubahan Master.

Pemeriksaan browser interaktif tidak tersedia karena layanan browser tidak merespons. Halaman dan respons form diperiksa melalui Django test client; JavaScript diperiksa dengan `node --check`.

## Berkas utama yang berubah

- Model/migrasi: `tracking/models.py`, `tracking/migrations/0010_material_usage_snapshots.py`, `0011_preserve_completed_production.py`.
- Perhitungan dan alur: `tracking/production_services.py`, `services.py`, `size_services.py`, `parsers.py`, `management/commands/seed_contoh.py`.
- Form/rute: `tracking/forms.py`, `tracking/views.py`, `config/urls.py`.
- Tampilan: `templates/material_usage.html`, `master.html`, `cmt_detail.html`, `po_detail.html`, `po_list.html`, `partials/production_estimates.html`, `tracking/templatetags/tracker.py`.
- JavaScript/CSS: `static/material-usage.js`, `production-estimates.js`, `production-sizes.js`, `app.css`.
- Tes: `tracking/test_material_usage.py` serta fixture/asumsi finalisasi pada `test_size_helpers.py`, `test_production_sizes.py`, `test_service_safety.py`, `tests.py`.
