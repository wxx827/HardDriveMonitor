# 打包单文件 exe：在独立虚拟环境里安装依赖，生成图标，用 PyInstaller 输出 dist\硬盘监视器.exe
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = ".build-venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    python -m venv .build-venv
    & $py -m pip install --upgrade pip
    & $py -m pip install "PySide6>=6.5,<6.9" numpy pyinstaller
}

& $py scripts\make_icon.py
& $py -m PyInstaller app.py --name 硬盘监视器 --onefile --windowed --noconfirm `
    --icon "$PWD\build\app.ico" --workpath build\pyi --specpath build --distpath dist `
    --exclude-module tkinter --exclude-module unittest --exclude-module tests `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick
Write-Host "完成：dist\硬盘监视器.exe"
