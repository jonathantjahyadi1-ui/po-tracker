$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    $taskLauncher = Get-Command py -ErrorAction SilentlyContinue
    $taskBundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $taskBundledPython) {
        & $taskBundledPython -m venv .venv
    } elseif ($taskLauncher) {
        & $taskLauncher.Source -3.12 -m venv .venv
    } else {
        throw 'Python 3.12 diperlukan.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Pembuatan virtual environment gagal.' }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Instalasi dependensi gagal.' }
}
$env:DEBUG = 'true'
& '.\.venv\Scripts\python.exe' manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Migrasi gagal.' }
& '.\.venv\Scripts\python.exe' manage.py runserver 127.0.0.1:8765
