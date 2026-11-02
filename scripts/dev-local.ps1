param(
    [string]$HostName = "0.0.0.0",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Cloud-only .pyc files in this OneDrive folder can hang Python on import.
if (-not $env:PYTHONPYCACHEPREFIX) {
    $env:PYTHONPYCACHEPREFIX = Join-Path $env:LOCALAPPDATA "pegasusDB-pyc"
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Virtual environment Python not found at $venvPython. Create it with: py -m venv .venv"
}

function Get-DevUrls {
    param([int]$Port)
    $urls = @(
        "http://127.0.0.1:$Port",
        "http://localhost:$Port"
    )
    try {
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.IPAddress -and
                $_.IPAddress -notmatch '^127\.' -and
                $_.IPAddress -notmatch '^169\.254\.'
            } |
            ForEach-Object { $urls += "http://$($_.IPAddress):$Port" }
    } catch {
        # Older Windows / restricted shells — LAN URL still printed by Django if needed
    }
    return $urls | Select-Object -Unique
}

$address = "$HostName`:$Port"
$env:DEV_SERVER_PORT = "$Port"

Write-Host ""
Write-Host "Starting Django dev server (listening on all interfaces)..." -ForegroundColor Cyan
Write-Host ""
Write-Host "Open in a browser:" -ForegroundColor Green
foreach ($url in (Get-DevUrls -Port $Port)) {
    Write-Host "  $url"
}
Write-Host ""
Write-Host "On your phone or tablet (same Wi-Fi), use the http://192.168... URL above." -ForegroundColor Yellow
Write-Host "If the page does not load, allow Python through Windows Firewall for port $Port." -ForegroundColor DarkYellow
Write-Host ""

& $venvPython manage.py runserver $address
