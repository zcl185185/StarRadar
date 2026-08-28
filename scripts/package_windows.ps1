# 构建 StarRadar Windows 桌面版（在项目根目录执行）。
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "未找到 .venv。请先运行：py -3.12 -m venv .venv"
}

Push-Location $projectRoot
try {
    # requirements 文件含 UTF-8 中文注释；显式启用 UTF-8，避免部分 Windows
    # 环境下 pip 按 GBK 读取而中断打包。
    & $python -X utf8 -m pip install -r requirements-desktop.txt
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name StarRadar `
        --add-data "static;static" `
        --collect-all webview `
        --collect-all sentence_transformers `
        --collect-all transformers `
        --hidden-import hnswlib `
        --hidden-import rank_bm25 `
        desktop.py
}
finally {
    Pop-Location
}

Write-Host "构建完成：dist\StarRadar\StarRadar.exe"
