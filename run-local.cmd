@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-local.ps1"
if errorlevel 1 (
    echo.
    echo Server gagal dijalankan. Pesan kesalahan ada di atas.
    pause
)
