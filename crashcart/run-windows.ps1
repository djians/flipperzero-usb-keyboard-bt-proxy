$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    throw "Not installed. Run .\install-windows.ps1 first."
}

& .\.venv\Scripts\python.exe .\crashcart_bridge.py @args
