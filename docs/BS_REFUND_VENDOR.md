# Bahan BS dan refund vendor

Implementasi PRD `PRD_PO_Tracker_BS_Bahan_dan_Refund_Vendor.md` versi 1.1,
22 Oktober 2026. Perubahan memperluas invoice, roll, CMT, produksi, dan
pembayaran existing.

## Penggunaan

1. Purchasing membuka **Master → Bahan**, mengisi **Harga per Yard (Rp)** yang
   positif, lalu menyimpan. Pemakaian paten produk dan patokan khusus bahan tetap
   tersedia. Harga bahan lama sengaja dibiarkan kosong sampai diisi pengguna.
2. Buka **CMT → detail CMT**. Pilih **Catat BS** pada roll yang sudah diterima,
   atau aksi satu/beberapa roll pada bagian **Catatan BS**. PO tidak wajib.
3. Pilih roll, isi yard BS, tanggal dan keterangan. **Seluruh sisa roll** mengisi
   yard layak pakai otomatis. Koma desimal didukung, misalnya `12,5`. Yard memakai
   maksimal enam angka pecahan; harga dan uang dua angka pecahan.
4. Tinjau harga dan dampak per invoice, lalu **Simpan BS**. Server memeriksa ulang
   kapasitas dan tagihan. Isian dipertahankan bila gagal. Pengiriman form yang sama
   tidak menggandakan transaksi.
5. Buka catatan BS untuk melihat sumber bahan, snapshot harga dan invoice terkait.
   Untuk setiap item yang benar-benar dikirim kembali, isi tanggal/keterangan
   melalui **Tandai Sudah Diretur**. Item lain tetap menunggu retur.
6. Saat uang benar-benar diterima, Direktur/Super Admin membuka **Payment →
   invoice → Catat Refund Diterima**. Isi nominal dan tanggal penerimaan sebenarnya;
   referensi opsional. Penerimaan bertahap diperbolehkan hingga sisa refund nol.

Badge merah lembut disertai teks BS/Sebagian BS. Status pembayaran dan refund
ditampilkan berdampingan di Payment dan detail vendor. Detail invoice memuat
rincian BS dan histori penerimaan. Retur fisik tidak mencatat uang masuk, dan
penerimaan refund tidak mengubah status retur.

## Perhitungan dan pilihan implementasi

- `Roll.yard` mempertahankan penerimaan awal. `Roll.bs_yard` menjadi cache
  transaksional jumlah item BS aktif. Yard layak pakai = yard awal − BS aktif.
- Item BS menyimpan yard, harga master saat pencatatan, hasil perkalian, serta
  invoice/vendor asal dan PO opsional. Perubahan master berikutnya tidak mengubah
  snapshot. Uang dibulatkan HALF_UP ke dua desimal **per item**, kemudian dijumlahkan.
- Item BS aktif adalah sumber penyesuaian invoice. Tagihan setelah BS = tagihan
  awal − BS aktif. Sisa dibayar = maksimum(tagihan setelah BS − pembayaran, 0).
  Refund perlu diminta = maksimum(pembayaran − tagihan setelah BS, 0). Sisa refund
  hanya mengurangi penerimaan aktif pada invoice tersebut.
- Pembayaran keluar dipertahankan; uang refund masuk disimpan terpisah. Invoice
  yang sudah Paid tetap Paid. Tidak ada saldo vendor atau pemotongan invoice baru.
- Harga kosong, jumlah di luar yard layak pakai, BS melampaui nilai invoice,
  serta invoice yang belum direkonsiliasi ditolak dengan pesan yang jelas.
- BS langsung memengaruhi stok/tagihan saat disimpan. Retur tidak mengurangi yard
  layak pakai atau tagihan untuk kedua kalinya. Seluruh sisa BS tidak dapat
  dialokasikan; setelah seluruhnya diretur, roll menjadi **Diretur ke vendor**.
- Estimasi produksi memakai yard setelah BS dan pembulatan PCS existing. Karena
  konsumsi yard per roll belum dicatat aplikasi, BS pada PO/bahan/warna yang sudah
  memiliki hasil produksi, kiriman, data produksi ambigu atau finalisasi diblokir
  sampai koreksi produksi selesai. Hasil PCS tidak diubah diam-diam.

### Milestone dan modul pembayaran yang tersedia

Repository memakai pembayaran invoice **Lunas/Cicil**, tanpa model jadwal
milestone/nominal termin yang belum dibayar. Potongan diterapkan ke sisa kewajiban
invoice tersebut. Kewajiban yang habis karena BS diberi penjelasan **Selesai dengan
potongan BS**. Tidak dibuat termin atau pembayaran fiktif dari riwayat transfer.

Urutan PRD dari termin terakhir belum mempunyai objek jadwal dalam schema ini.
Implementasi mengikuti arahan PRD untuk menyesuaikan dengan modul pembayaran
yang tersedia. Bila jadwal termin ditambahkan kelak, potongan harus dimulai dari
sisa termin terakhir, tanpa mengubah transaksi kas.

## Koreksi dan konsistensi

Super Admin dapat **Batalkan BS** dengan alasan dan konfirmasi. Histori tetap
disimpan; yard dan tagihan dibalik dalam satu transaksi. Pembatalan ditolak jika
ada retur fisik, bahan sudah diproduksi, atau hak refund sesudah pembatalan lebih
kecil daripada uang yang sudah diterima. Koreksi lokasi retur belum tersedia,
sehingga BS yang sudah diretur tetap terkunci.

Kesalahan penerimaan dikoreksi Super Admin melalui **Batalkan penerimaan** dengan
alasan; catatan lama tetap tersimpan. Koreksi ini bukan transfer uang keluar.

Penulisan BS mengunci alokasi, invoice, roll, lalu PO dalam urutan tetap. Refund
dan pembayaran keluar menggunakan kunci invoice yang sama. Request ID unik dan
fingerprint melindungi replay. Kegagalan stok, tagihan atau log membatalkan seluruh
transaksi. PostgreSQL memakai penguncian baris; SQLite memakai reservasi penulisan.
Membuka halaman, ekspor, dan retur tidak membuat transaksi keuangan tambahan.

`manage.py audit_workflow --json` menambahkan `bs_stock_mismatches` dan
`refund_exceeds_entitlement` untuk membandingkan cache dengan sumber transaksi.

## Ekspor

Excel invoice menambah yard BS dan yard layak pakai. Jika ada histori BS/refund,
unduhan invoice maupun Payment menambahkan **Posisi BS dan Refund**, **Rincian BS**,
dan **Penerimaan Refund**. Catatan batal tetap terlihat; ringkasan menghitung hanya
transaksi aktif. Sheet pembayaran keluar dan file asli invoice vendor tetap ada.

## Performa

Detail vendor memakai agregasi database tanpa memuat semua objek roll. Daftar PO
tanpa filter status menghitung hanya 30 PO halaman aktif; filter status tetap
menghitung semua kandidat agar hasilnya akurat. Pasangan bahan/warna PO memakai
DISTINCT. Pengiriman CMT memakai prefetch relasi gabungan; PO terkait menampilkan
20 terbaru dengan tautan daftar lengkap. Master dipaginasi 50. Status retur roll
dan ringkasan BS multi-invoice dimuat bersama. Font eksternal dihapus, dan panel
pengiriman di luar layar memakai `content-visibility`.

Benchmark sintetis lokal terpisah: **1.000 PO, 20.000 roll, 20 invoice**, median
lima render. Fungsi daftar lama dibaca dari Git HEAD, dengan resolver bersama dan
template yang sama untuk membandingkan pemuatan daftar/paginasi.

| Halaman | Sebelum | Sesudah | Pengurangan waktu |
|---|---:|---:|---:|
| Detail vendor | 1.619,76 ms / 5 query | 125,63 ms / 3 query | 92,2% |
| PO per CMT | 156,53 ms / 9 query | 39,82 ms / 9 query | 74,6% |

Ini waktu render SQLite lokal, bukan pengukuran jaringan/browser atau jaminan
latensi produksi. Jalankan ulang dengan `python scripts/benchmark_navigation.py`.
Data/laporan dibuat hanya di `.verification/`. Baseline mengikuti HEAD saat script
dijalankan, sehingga sesudah commit baru baseline tersebut juga berubah.

## Migrasi dan verifikasi, 22 Oktober 2026

Migrasi `0013_fabric_bs_vendor_refund` menambah harga nullable, cache BS nol,
catatan/item BS dan penerimaan refund. Tidak mengisi harga fiktif atau mengubah
status rusak/retur lama menjadi potongan/penerimaan uang otomatis.

Audit salinan database aktif mempertahankan semua nilai field lama, transaksi dan
akun: **1 invoice, 15 roll, 1.773 yard, 1.072 hasil PCS, 1.072 kiriman PCS, 78 log**.
Pembandingan memakai nama kolom dalam urutan stabil karena SQLite dapat mengubah
urutan fisik kolom saat membangun ulang tabel. `local.sqlite3` memakai schema
aplikasi lain dan tidak dimigrasikan.

Setelah audit lulus, `severli.sqlite3` lokal dimigrasikan ke `0013` dengan backup
konsisten `.verification/severli-before-bs-20261022-a2755c86.sqlite3`. Semua field
lama tetap sama; audit BS/refund bersih. Harga bahan lama perlu diisi Purchasing.

Suite penuh: **214 tes, 204 lulus, 10 dilewati**. Skenario meliputi posisi invoice
PRD, snapshot, yard sebagian/penuh, beberapa vendor, replay, retur satu kali,
refund bertahap/tambahan, invoice berikutnya mandiri, transaksi atomik, hak akses
HTTP, koreksi, ekspor dan paginasi. Setelah perubahan terakhir, 127 tes terkait
workflow/master/BS dijalankan kembali: **123 lulus, 4 PostgreSQL dilewati**.

Pemeriksaan Django, kesesuaian migrasi, lint Python dan sintaks JavaScript lulus.
Sebanyak 12 halaman/ekspor Payment pada database lokal aktif juga lulus melalui
koneksi read-only. Lima belas halaman/ekspor workflow utama juga lulus dengan
`PRAGMA query_only=ON`; seluruh template berhasil dikompilasi.

**Batas verifikasi:** sepuluh tes penguncian PostgreSQL, termasuk dua tes baru
BS/refund bersamaan, belum dijalankan karena PostgreSQL tes tidak tersedia.
Pembuktian transaksi bersamaan A16/A17 memerlukan database tes tersebut. Browser
tidak tersedia melalui konektor sesi; QA visual/interaksi langsung pada ukuran
layar berbeda belum dijalankan. Render HTML dan form HTTP telah diuji.

Tidak ada deploy atau migrasi database produksi. Sebelum rilis, audit salinan
database produksi, migrasikan schema yang sama, jalankan tes PostgreSQL melalui
`TEST_DATABASE_URL` terpisah, dan periksa tampilan di browser. Jika lingkungan
lain memiliki saldo vendor lintas invoice, tinjau datanya dahulu; repository yang
diperiksa tidak mempunyai model saldo tersebut.
