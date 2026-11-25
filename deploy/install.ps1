# One-line Windows installer for the 4-bot Bybit demo system.
# Usage (PowerShell):
#   $env:APP_TOKEN='your-code'; irm https://raw.githubusercontent.com/meirvbb-png/cryptppilot-ai/claude/computer-check-optimization-l2lweg/deploy/install.ps1 | iex
# Installs to Documents\CryptoBots, keeps existing data (.env, app_data) on re-run,
# adds a Startup shortcut (starts minimized at log-on) and prints the phone address.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Branch = 'claude/computer-check-optimization-l2lweg'
$Zip    = "https://github.com/meirvbb-png/cryptppilot-ai/archive/refs/heads/$Branch.zip"
$Dir    = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'CryptoBots'
$Token  = if ($env:APP_TOKEN) { $env:APP_TOKEN } else { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 14 | ForEach-Object { [char]$_ }) }

function Say($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }

# ---------- 1. Python ----------
function Find-Python {
    foreach ($c in @('py', 'python')) {
        try {
            $v = & $c -3 --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3\.(1[1-9])') { return $c }
        } catch {}
        try {
            $v = & $c --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3\.(1[1-9])') { return $c }
        } catch {}
    }
    return $null
}
Say 'Checking Python...'
$py = Find-Python
if (-not $py) {
    Say 'Installing Python 3.12 (a Windows permission window may appear - click Yes)...'
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $py = Find-Python
    if (-not $py) { throw 'Python was installed but not found. Close this window, open PowerShell again and run the same line again.' }
}
Write-Host "Using: $(& $py --version)"

# ---------- 2. Code ----------
Say "Downloading the system to $Dir ..."
$tmp = Join-Path $env:TEMP ("bots-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
Invoke-WebRequest $Zip -OutFile "$tmp\src.zip"
Expand-Archive "$tmp\src.zip" -DestinationPath $tmp -Force
$src = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like 'cryptppilot-ai-*' } | Select-Object -First 1
New-Item -ItemType Directory -Path $Dir -Force | Out-Null
foreach ($p in @('app', 'research', 'deploy', 'requirements.txt', 'requirements-research.txt', 'BOTS_README.md', '.env.example', 'start.bat')) {
    $s = Join-Path $src.FullName $p
    if (Test-Path $s) { Copy-Item $s -Destination $Dir -Recurse -Force }
}
Remove-Item $tmp -Recurse -Force

# ---------- 3. Libraries ----------
Set-Location $Dir
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Say 'Creating Python environment...'
    if ($py -eq 'py') { & py -3 -m venv .venv } else { & python -m venv .venv }
}
Say 'Installing libraries (a few minutes the first time)...'
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw 'Library installation failed (see the messages above).' }

# ---------- 4. Settings ----------
if (-not (Test-Path '.env')) {
    $lines = [IO.File]::ReadAllLines((Join-Path $Dir '.env.example'), [Text.Encoding]::UTF8) -replace '^APP_TOKEN=.*', "APP_TOKEN=$Token"
    [IO.File]::WriteAllLines((Join-Path $Dir '.env'), $lines, (New-Object Text.UTF8Encoding($false)))   # UTF-8 without BOM
} else {
    $Token = ((Get-Content '.env' -Encoding UTF8 | Where-Object { $_ -match '^APP_TOKEN=' }) -replace '^APP_TOKEN=', '').Trim()
}

# ---------- 5. Start script + auto-start at log-on ----------
@"
@echo off
cd /d "%~dp0"
set MALLOC_ARENA_MAX=2
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m app
"@ | Set-Content (Join-Path $Dir 'run-bots.bat') -Encoding ASCII
$startup = [Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $startup 'CryptoBots.lnk'))
$lnk.TargetPath = Join-Path $Dir 'run-bots.bat'
$lnk.WorkingDirectory = $Dir
$lnk.WindowStyle = 7
$lnk.Save()

# ---------- 6. Start now and verify ----------
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*$Dir*-m app*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Say 'Starting the bots (Windows may ask about the firewall - click Allow)...'
Start-Process -FilePath (Join-Path $Dir 'run-bots.bat') -WorkingDirectory $Dir -WindowStyle Minimized
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-RestMethod 'http://localhost:8000/api/ping' -TimeoutSec 5
        if ($r.ready) { $ok = $true; break }
    } catch {}
}
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet|VirtualBox|VMware' -and $_.IPAddress -notlike '169.*' } |
       Select-Object -First 1).IPAddress

if ($ok) {
    Write-Host "`n============================================================" -ForegroundColor Green
    Write-Host "  READY. Open on your phone (same Wi-Fi):  http://${ip}:8000" -ForegroundColor Green
    Write-Host "  On this computer:                        http://localhost:8000" -ForegroundColor Green
    Write-Host "  Access code:                             $Token" -ForegroundColor Green
    Write-Host "  Auto-start at log-on: ON (Startup folder shortcut 'CryptoBots')" -ForegroundColor Green
    Write-Host "============================================================`n" -ForegroundColor Green
} else {
    Write-Host "`nThe system did not answer yet. Open the minimized 'run-bots' window to see the error, or send a screenshot." -ForegroundColor Yellow
}
