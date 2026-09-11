$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($env:DATABASE_URL) { throw 'Pratinjau lokal harus memakai database contoh. Jalankan di terminal tanpa DATABASE_URL.' }
if ((Test-Path -LiteralPath '.env') -and (Select-String -LiteralPath '.env' -Pattern '^\s*DATABASE_URL\s*=\s*\S' -Quiet)) {
    throw 'File .env berisi DATABASE_URL. Gunakan folder salinan tanpa koneksi Supabase untuk demo lokal.'
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    $taskLauncher = Get-Command py -ErrorAction SilentlyContinue
    $taskBundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if ($taskLauncher) {
        & $taskLauncher.Source -3.12 -m venv .venv
    } elseif (Test-Path -LiteralPath $taskBundledPython) {
        & $taskBundledPython -m venv .venv
    } else {
        throw 'Pasang Python 3.12 dan Python Launcher terlebih dahulu.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 diperlukan.' }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Instalasi dependensi gagal.' }
}
$env:DEBUG = 'true'
& '.\.venv\Scripts\python.exe' manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Migrasi gagal.' }
& '.\.venv\Scripts\python.exe' manage.py seed_demo
if ($LASTEXITCODE -ne 0) { throw 'Seed demo gagal.' }
& '.\.venv\Scripts\python.exe' manage.py runserver 127.0.0.1:8765
