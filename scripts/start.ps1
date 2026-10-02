#Requires -Version 5.1
<#
.SYNOPSIS
    One-command startup for the whole RAKSHAK demo.

.DESCRIPTION
    Starts PostgreSQL, Redis, the FastAPI backend, the citizen web app, the
    police dashboard and the Expo mobile app (host only), then prints the URLs
    and the demo credentials.

    Two modes, auto-selected:
      docker - everything except the mobile app runs in containers.
      local  - everything runs on this machine against the already installed
               PostgreSQL/Redis (no admin rights needed).

    The LAN IPv4 of this PC is detected once and used for every generated URL,
    so a physical phone on the same Wi-Fi can reach the backend and Metro.

.PARAMETER Mode
    auto (default), docker or local.

.PARAMETER HostIp
    Override the detected LAN IPv4 address, e.g. -HostIp 192.168.1.20

.PARAMETER Rebuild
    Force image rebuild (docker) / reinstall + Expo cache clear (local).

.PARAMETER NoMobile
    Do not start Expo Metro.

.PARAMETER Reset
    Stop everything and wipe state: docker volumes, or drop and recreate the
    dev database (local). Asks for confirmation first.

.EXAMPLE
    .\scripts\start.ps1
    .\scripts\start.ps1 -Mode local -NoMobile
    .\scripts\start.ps1 -Reset
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'docker', 'local')]
    [string] $Mode = 'auto',

    [string] $HostIp = '',

    [switch] $Rebuild,

    [switch] $NoMobile,

    [switch] $Reset,

    [switch] $ForceSeed,

    [switch] $SkipFirewall
)

# PowerShell 5.1 compatible on purpose: no ??, no ternary, no -AsHashtable.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
$ScriptDir = $PSScriptRoot
if ([string]::IsNullOrEmpty($ScriptDir)) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$RepoRoot = Split-Path -Parent $ScriptDir
$StateDir = Join-Path $RepoRoot '.rakshak'
$LogDir = Join-Path $RepoRoot 'logs'
$EnvPath = Join-Path $RepoRoot '.env'
$EnvExamplePath = Join-Path $RepoRoot '.env.example'
$PidsPath = Join-Path $StateDir 'pids.json'
$HashesPath = Join-Path $StateDir 'hashes.json'
$SeedMarkerPath = Join-Path $StateDir 'seed.done'

$BackendDir = Join-Path $RepoRoot 'backend'
$CitizenDir = Join-Path $RepoRoot 'citizen_web'
$PoliceDir = Join-Path $RepoRoot 'police_dashboard'
$MobileDir = Join-Path $RepoRoot 'mobile_app'
$VenvDir = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

$Ports = @{ backend = 8000; citizen = 5173; police = 5174; expo = 8081 }

$script:Completed = $false
$script:LanIp = $null
$script:EffectiveMode = 'local'
$script:Fresh = $true     # $false => every tracked service was already running
$script:ExpoPending = $false
$script:ExpoRunning = $false
$script:PhoneState = 'not-checked'   # authorized | unauthorized | none | no-adb
$script:AdbDownloaded = $false

# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
function Write-Banner {
    param([string]$Text, [string]$Color = 'Cyan')
    Write-Host ''
    Write-Host ("=" * 74) -ForegroundColor DarkGray
    Write-Host ("  " + $Text) -ForegroundColor $Color
    Write-Host ("=" * 74) -ForegroundColor DarkGray
}

function Write-Step { param([string]$Text) Write-Host ("  -> " + $Text) -ForegroundColor Cyan }
function Write-Ok { param([string]$Text) Write-Host ("  [ OK ] " + $Text) -ForegroundColor Green }
function Write-Info { param([string]$Text) Write-Host ("  [info] " + $Text) -ForegroundColor Gray }
function Write-Warn2 { param([string]$Text) Write-Host ("  [warn] " + $Text) -ForegroundColor Yellow }
function Write-Err { param([string]$Text) Write-Host ("  [FAIL] " + $Text) -ForegroundColor Red }

function Die {
    param([string]$Text)
    Write-Err $Text
    Write-Host ''
    exit 1
}

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
function Write-TextFile {
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    # UTF-8 without BOM, LF endings: safe for dotenv parsers and for bash.
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Text -replace "`r`n", "`n"), $utf8)
}

function Get-FileHashHex {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash } catch { return '' }
}

function Get-CombinedHash {
    param([string[]] $Paths)
    $joined = ($Paths | ForEach-Object { "$_=" + (Get-FileHashHex $_) }) -join '|'
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    return [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '')
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Write-JsonFile {
    param([string]$Path, $Object)
    Write-TextFile $Path (($Object | ConvertTo-Json -Depth 6) + "`n")
}

function Get-StateMap {
    param([string]$Path, [string]$Property)
    $map = @{}
    $json = Read-JsonFile $Path
    if ($null -eq $json) { return $map }
    $holder = $null
    if ($Property -and $json.PSObject.Properties.Name -contains $Property) { $holder = $json.$Property }
    else { $holder = $json }
    if ($null -eq $holder) { return $map }
    foreach ($prop in $holder.PSObject.Properties) {
        $map[$prop.Name] = $prop.Value
    }
    return $map
}

# --- .env handling (never clobber user values) --------------------------------
function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match '^\s*' + [regex]::Escape($Key) + '\s*=(.*)$') {
            return $Matches[1].Trim()
        }
    }
    return $null
}

function Set-DotEnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in [System.IO.File]::ReadAllLines($Path)) { $lines.Add($line) }
    }
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*' + [regex]::Escape($Key) + '\s*=') {
            $lines[$i] = "$Key=$Value"
            $found = $true
            break
        }
    }
    if (-not $found) {
        if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -ne '') { $lines.Add('') }
        $lines.Add("# --- generated by scripts/start.ps1 ---")
        $lines.Add("$Key=$Value")
    }
    Write-TextFile $Path (($lines -join "`n") + "`n")
}

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $name = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        if ($name -and $name -notmatch '^#') {
            [System.Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}

# --- process helpers ----------------------------------------------------------
function Get-ListeningPid {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch { }
    try {
        $line = & netstat.exe -ano -p TCP 2>$null | Select-String (":$Port\s+.*LISTENING")
        if ($line) {
            $parts = ($line.ToString() -split '\s+') | Where-Object { $_ -ne '' }
            return [int]$parts[-1]
        }
    } catch { }
    return 0
}

function Get-ProcessInfo {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $null }
    try { return Get-Process -Id $ProcessId -ErrorAction SilentlyContinue } catch { return $null }
}

function Test-ProcessAlive {
    param([int]$ProcessId)
    return $null -ne (Get-ProcessInfo $ProcessId)
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return }
    if (-not (Test-ProcessAlive $ProcessId)) { return }
    try {
        & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
    } catch { }
    Start-Sleep -Milliseconds 400
    if (Test-ProcessAlive $ProcessId) {
        try { Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
}

function Test-TcpPort {
    param([string]$Host_, [int]$Port, [int]$TimeoutMs = 1500)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($Host_, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch { }
    }
}

function Invoke-HttpOk {
    param([string]$Url, [int]$TimeoutSec = 3)
    try {
        [System.Net.ServicePointManager]::Expect100Continue = $false
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -Method GET
        return ([int]$resp.StatusCode -ge 200 -and [int]$resp.StatusCode -lt 400)
    } catch {
        return $false
    }
}

function Wait-ForHttp {
    param([string]$Url, [int]$TimeoutSec, [string]$Label)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Invoke-HttpOk -Url $Url -TimeoutSec 3) { return $true }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

function Test-Command {
    param([string]$Name)
    try {
        $cmd = Get-Command $Name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    } catch { }
    return $null
}

function Test-Admin {
    try {
        $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
        return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Confirm-Question {
    param([string]$Text, [switch]$DefaultYes)
    $suffix = 'y/N'
    if ($DefaultYes) { $suffix = 'Y/n' }
    try {
        $answer = Read-Host "  $Text ($suffix)"
    } catch {
        return [bool]$DefaultYes
    }
    if ([string]::IsNullOrWhiteSpace($answer)) { return [bool]$DefaultYes }
    return ($answer.Trim().ToLower() -eq 'y' -or $answer.Trim().ToLower() -eq 'yes')
}

# --------------------------------------------------------------------------- #
# Step 1 - prerequisites
# --------------------------------------------------------------------------- #
function Get-NodeMajor {
    $node = Test-Command 'node'
    if (-not $node) { return 0 }
    try {
        $out = & $node --version 2>$null
        $ver = ($out | Select-Object -First 1).TrimStart('v')
        return [int]($ver.Split('.')[0])
    } catch { return 0 }
}

# The interpreter used to build/manage .venv. Deliberately never the venv's own
# python: recreating the venv must not use an interpreter it is about to delete.
function Get-BasePythonCommand {
    $py = Test-Command 'python'
    if ($py) { return $py }
    $py3 = Test-Command 'py'
    if ($py3) { return $py3 }
    return $null
}

function Get-PythonCommand {
    if (Test-Path -LiteralPath $VenvPython) { return $VenvPython }
    return (Get-BasePythonCommand)
}

function Get-PythonVersionString {
    $python = Get-PythonCommand
    if (-not $python) { return '' }
    try {
        $out = & $python -c "import sys;print('%d.%d' % (sys.version_info[0], sys.version_info[1]))" 2>$null
        return ($out | Select-Object -First 1)
    } catch { return '' }
}

function Resolve-PsqlPath {
    $candidates = @()
    $found = Test-Command 'psql'
    if ($found) { $candidates += $found }
    foreach ($base in @('C:\Program Files\PostgreSQL', 'C:\Program Files (x86)\PostgreSQL')) {
        if (Test-Path -LiteralPath $base) {
            Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object {
                    $p = Join-Path $_.FullName 'bin\psql.exe'
                    if (Test-Path -LiteralPath $p) { $candidates += $p }
                }
        }
    }
    if ($candidates.Count -gt 0) { return $candidates[0] }
    return $null
}

function Resolve-RedisCliPath {
    $found = Test-Command 'redis-cli'
    if ($found) { return $found }
    foreach ($p in @('C:\Program Files\Redis\redis-cli.exe')) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Invoke-Psql {
    param([string]$Psql, [string]$Sql, [string]$User, [string]$Password, [string]$DbHost = 'localhost', [int]$Port = 5432, [string]$DbName = 'postgres')
    $env:PGPASSWORD = $Password
    $out = & $Psql -h $DbHost -p $Port -U $User -d $DbName -w -t -A -c $Sql 2>&1
    return $out
}

function Test-PostgresReachable {
    $user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { 'postgres' }
    $pass = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { 'postgres' }
    $psql = Resolve-PsqlPath
    if ($psql) {
        $res = Invoke-Psql -Psql $psql -Sql 'SELECT 1;' -User $user -Password $pass -DbHost 'localhost' -Port 5432 -DbName 'postgres'
        return (($res | Out-String) -match '\s1\s*$' -or (($res | Out-String).Trim() -eq '1'))
    }
    return (Test-TcpPort -Host_ 'localhost' -Port 5432)
}

function Test-RedisReachable {
    $cli = Resolve-RedisCliPath
    if ($cli) {
        $res = & $cli -h 127.0.0.1 -p 6379 ping 2>&1
        return ((($res | Out-String).Trim()) -match 'PONG')
    }
    return (Test-TcpPort -Host_ '127.0.0.1' -Port 6379)
}

function Start-WindowsServiceIfStopped {
    param([string[]] $Patterns)
    foreach ($pattern in $Patterns) {
        $svc = Get-Service -Name $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $svc) { continue }
        if ($svc.Status -eq 'Running') { continue }
        Write-Info ("starting Windows service '{0}'..." -f $svc.Name)
        try {
            Start-Service -Name $svc.Name -ErrorAction Stop
            Write-Ok ("service '{0}' running" -f $svc.Name)
        } catch {
            Write-Warn2 ("could not start service '{0}': {1}" -f $svc.Name, $_.Exception.Message)
            Write-Warn2 ("run as Administrator:  Start-Service {0}" -f $svc.Name)
        }
        return
    }
}

function Assert-Prerequisites {
    param([string]$InMode)
    Write-Step 'Checking prerequisites'

    $nodeMajor = Get-NodeMajor
    if ($nodeMajor -lt 18) {
        if ($nodeMajor -eq 0) {
            Die "Node.js 18+ is required but was not found on PATH. Install it from https://nodejs.org (LTS) and reopen this window."
        }
        Die "Node.js $nodeMajor is too old. Node.js 18+ is required - install LTS from https://nodejs.org"
    }
    if (-not (Test-Command 'npm')) {
        Die 'npm was not found on PATH. Reinstall Node.js LTS (npm ships with it) and reopen this window.'
    }
    Write-Ok ("Node.js {0} + npm" -f $nodeMajor)

    if ($InMode -eq 'docker') {
        $docker = Test-Command 'docker'
        if (-not $docker) {
            Die 'Docker mode was requested but the "docker" command is not on PATH. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), or run without -Mode docker for the local mode.'
        }
        Write-Ok 'Docker CLI found'
    } else {
        $py = Get-PythonCommand
        if (-not $py) {
            Die 'Python 3.11+ is required for local mode but "python" was not found on PATH. Install it from https://www.python.org/downloads/ (tick "Add to PATH"), or use -Mode docker.'
        }
        $pyVer = Get-PythonVersionString
        $parts = $pyVer.Split('.')
        if ($parts.Count -lt 2) { Die "Could not read the Python version (got '$pyVer')." }
        $major = [int]$parts[0]; $minor = [int]$parts[1]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
            Die "Python $pyVer is too old. Local mode needs Python 3.11+ (3.12 recommended)."
        }
        Write-Ok ("Python $pyVer")

        if (-not (Test-PostgresReachable)) {
            Write-Warn2 'PostgreSQL is not answering on localhost:5432'
            Start-WindowsServiceIfStopped -Patterns @('postgresql-x64-17', 'postgresql-x64-16', 'postgresql-x64-15', 'postgresql-x64-14', 'postgresql*')
            Start-Sleep -Seconds 3
            if (-not (Test-PostgresReachable)) {
                Die "PostgreSQL is not reachable on localhost:5432. Start it, then re-run. Docker alternative: start PostgreSQL with  docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:17-alpine"
            }
        }
        Write-Ok 'PostgreSQL reachable on localhost:5432'

        if (-not (Test-RedisReachable)) {
            Write-Warn2 'Redis is not answering on localhost:6379'
            Start-WindowsServiceIfStopped -Patterns @('Redis')
            Start-Sleep -Seconds 2
            if (-not (Test-RedisReachable)) {
                Die "Redis is not reachable on localhost:6379. Start it, then re-run. Docker alternative: start Redis with  docker run -d -p 6379:6379 redis:7-alpine"
            }
        }
        Write-Ok 'Redis reachable on localhost:6379'
    }
}

# --------------------------------------------------------------------------- #
# Step 2 - LAN IP detection
# --------------------------------------------------------------------------- #
function Get-LanIpv4 {
    param([string] $Override)

    if ($Override) {
        if ($Override -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
            Die "-HostIp '$Override' is not a valid IPv4 address (example: -HostIp 192.168.1.20)"
        }
        Write-Info ("using -HostIp override: {0}" -f $Override)
        return $Override
    }

    $excluded = 'vEthernet|VirtualBox|VMware|WSL|Loopback|Hyper-V|Docker|WSL2|vbox|vmnet|TAP|Tailscale|ZeroTier|Loopback Pseudo'
    $candidates = New-Object System.Collections.Generic.List[object]

    try {
        $configs = Get-NetIPConfiguration -ErrorAction Stop
        foreach ($cfg in $configs) {
            if (-not $cfg.IPv4Address) { continue }
            $alias = [string]$cfg.InterfaceAlias
            if ($alias -match $excluded) { continue }
            $hasGateway = ($null -ne $cfg.IPv4DefaultGateway)
            foreach ($v4 in $cfg.IPv4Address) {
                $ip = [string]$v4.IPAddress
                if (-not $ip) { continue }
                if ($ip.StartsWith('127.')) { continue }      # loopback
                if ($ip.StartsWith('169.254.')) { continue }  # APIPA / no DHCP
                $score = 0
                if ($hasGateway) { $score += 100 }            # default gateway => real LAN
                if ($ip.StartsWith('192.168.')) { $score += 20 }
                elseif ($ip.StartsWith('10.')) { $score += 15 }
                elseif ($ip -match '^172\.(1[6-9]|2\d|3[01])\.') { $score += 10 }
    if ($alias -match 'Wi-Fi|Wireless|WLAN|WiFi|Ethernet') { $score += 5 }
                $candidates.Add([pscustomobject]@{ Ip = $ip; Alias = $alias; Gateway = $hasGateway; Score = $score })
            }
        }
    } catch {
        Write-Warn2 'Get-NetIPConfiguration failed, falling back to ipconfig parsing.'
    }

    if ($candidates.Count -eq 0) {
        try {
            $raw = & ipconfig.exe 2>$null | Out-String
            $current = ''
            foreach ($line in ($raw -split "`r?`n")) {
                if ($line -match '^(.+?)\s*:.*adapter\s*$') { $current = $Matches[1].Trim() }
                if ($line -match '^\s*IPv4 Address.*:\s*(\d{1,3}(\.\d{1,3}){3})') {
                    $ip = $Matches[1]
                    if ($ip.StartsWith('169.254.')) { continue }
                    if ($ip.StartsWith('127.')) { continue }
                    if ($current -match $excluded) { continue }
                    $candidates.Add([pscustomobject]@{ Ip = $ip; Alias = $current; Gateway = $false; Score = 1 })
                }
            }
        } catch { }
    }

    if ($candidates.Count -eq 0) {
        Write-Warn2 'Could not detect a LAN IPv4 address automatically.'
        Write-Warn2 'Falling back to 127.0.0.1 - a phone will NOT be able to reach the demo.'
        Write-Warn2 'Re-run with an explicit address if needed:  .\start.bat -HostIp 192.168.1.20'
        return '127.0.0.1'
    }

    $best = $candidates | Sort-Object -Property Score -Descending | Select-Object -First 1
    if ($candidates.Count -gt 1) {
        $others = ($candidates | Where-Object { $_.Ip -ne $best.Ip } | ForEach-Object { "$($_.Ip) ($($_.Alias))" }) -join ', '
        Write-Info ("other adapters ignored: {0}" -f $others)
    }
    return $best.Ip
}

# --------------------------------------------------------------------------- #
# Step 3 + 4 - env files
# --------------------------------------------------------------------------- #
function Initialize-EnvFiles {
    param([string] $Ip)

    $apiBase = "http://${Ip}:8000"
    $wsBase = "ws://${Ip}:8000"

    if (-not (Test-Path -LiteralPath $EnvPath)) {
        if (Test-Path -LiteralPath $EnvExamplePath) {
            Copy-Item -LiteralPath $EnvExamplePath -Destination $EnvPath -Force
            Write-Ok ('created .env from .env.example')
        } else {
            Write-Warn2 '.env.example is missing - writing a minimal .env'
            Write-TextFile $EnvPath @"
# --- PostgreSQL ---
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=rakshak
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/rakshak

# --- Redis ---
REDIS_URL=redis://localhost:6379/0

# --- Auth ---
JWT_SECRET=rakshak-dev-secret-change-in-production
JWT_ACCESS_MINUTES=30
JWT_REFRESH_DAYS=7

# --- Hotlist / Demo ---
HOTLIST_CONFIRMATION_HOURS=24
DEMO_MODE=true

# --- CORS ---
CORS_ORIGINS=http://localhost:5173,http://localhost:5174

# --- Storage ---
STORAGE_BACKEND=local
STORAGE_LOCAL_PATH=./data/uploads

# --- AI (optional local model paths) ---
AI_MODEL_PATH=
OCR_MODEL_PATH=

# --- Frontend ---
API_BASE_URL=http://localhost:8000
"@
        }
    } else {
        Write-Info '.env already exists - existing values are preserved'
    }

    # Only these generated keys are rewritten; everything else the user typed stays.
    $cors = "http://localhost:5173,http://localhost:5174,http://${Ip}:5173,http://${Ip}:5174"
    Set-DotEnvValue -Path $EnvPath -Key 'CORS_ORIGINS' -Value $cors
    Set-DotEnvValue -Path $EnvPath -Key 'API_BASE_URL' -Value $apiBase
    Set-DotEnvValue -Path $EnvPath -Key 'VITE_API_URL' -Value $apiBase
    Set-DotEnvValue -Path $EnvPath -Key 'VITE_WS_URL' -Value $wsBase
    Set-DotEnvValue -Path $EnvPath -Key 'EXPO_PUBLIC_API_URL' -Value $apiBase
    Write-Ok ('.env updated: CORS_ORIGINS, API_BASE_URL, VITE_API_URL, VITE_WS_URL, EXPO_PUBLIC_API_URL -> {0}' -f $apiBase)

    # Frontend env files: the LAN IP so the PC browser AND the phone both work.
    Write-TextFile (Join-Path $CitizenDir '.env.local') @"
# Generated by scripts/start.ps1 - the host the *browser* uses.
# Do not use "backend:8000" here: that name only resolves inside Docker.
VITE_API_URL=$apiBase
VITE_WS_URL=$wsBase
"@
    Write-TextFile (Join-Path $PoliceDir '.env.local') @"
# Generated by scripts/start.ps1 - the host the *browser* uses.
# Do not use "backend:8000" here: that name only resolves inside Docker.
VITE_API_URL=$apiBase
VITE_WS_URL=$wsBase
"@
    Write-TextFile (Join-Path $MobileDir '.env') @"
# Generated by scripts/start.ps1. EXPO_PUBLIC_* is inlined into the JS bundle at
# Metro start time, so the phone gets the PC's LAN IP instead of localhost.
EXPO_PUBLIC_API_URL=$apiBase
"@
    Write-Ok 'wrote citizen_web/.env.local, police_dashboard/.env.local, mobile_app/.env'

    Import-DotEnv -Path $EnvPath
}

# --------------------------------------------------------------------------- #
# Step 5 - firewall
# --------------------------------------------------------------------------- #
function Initialize-Firewall {
    param([string] $Ip)

    $definitions = @(
        @{ Port = 8000; Name = 'RAKSHAK-API-8000';  What = 'FastAPI backend' },
        @{ Port = 5173; Name = 'RAKSHAK-CITIZEN-5173'; What = 'citizen web' },
        @{ Port = 5174; Name = 'RAKSHAK-POLICE-5174';  What = 'police dashboard' },
        @{ Port = 8081; Name = 'RAKSHAK-EXPO-8081';   What = 'Expo Metro (QR code)' }
    )

    $isAdmin = Test-Admin
    if ($SkipFirewall) {
        Write-Info 'firewall step skipped (-SkipFirewall)'
        return
    }
    if (-not $isAdmin) {
        Write-Warn2 'not running as Administrator - firewall rules were NOT added'
        Write-Warn2 'the phone can still connect if Windows Firewall is off or the node/Private profile prompts "Allow access"'
        Write-Warn2 'to allow it once, open PowerShell as Administrator and run:'
        $oneLiner = ($definitions | ForEach-Object {
            "New-NetFirewallRule -DisplayName '$($_.Name)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $($_.Port) -Profile Private,Public"
        }) -join '; '
        Write-Host ("      " + $oneLiner) -ForegroundColor DarkYellow
        return
    }

    foreach ($def in $definitions) {
        try {
            $existing = Get-NetFirewallRule -DisplayName $def.Name -ErrorAction SilentlyContinue
            if ($existing) {
                Write-Info ("firewall rule '{0}' already exists" -f $def.Name)
                continue
            }
            New-NetFirewallRule -DisplayName $def.Name -Direction Inbound -Action Allow -Protocol TCP `
                -LocalPort $def.Port -Profile Private,Public -Description "RAKSHAK demo: $($def.What)" | Out-Null
            Write-Ok ("firewall rule '{0}' opened on TCP {1}" -f $def.Name, $def.Port)
        } catch {
            Write-Warn2 ("could not add firewall rule '{0}': {1}" -f $def.Name, $_.Exception.Message)
        }
    }
}

# --------------------------------------------------------------------------- #
# Process tracking (local mode)
# --------------------------------------------------------------------------- #
function Get-PidMap {
    return Get-StateMap -Path $PidsPath -Property 'services'
}

function Save-PidMap {
    param([hashtable] $Map)
    Write-JsonFile $PidsPath ([pscustomobject]@{
        mode = $script:EffectiveMode
        savedAt = (Get-Date).ToString('o')
        services = $Map
    })
}

function Test-ServiceRunning {
    param([string] $Name)
    $map = Get-PidMap
    if (-not $map.ContainsKey($Name)) { return $false }
    $entry = $map[$Name]
    $procId = 0
    if ($entry -is [int] -or $entry -is [long]) { $procId = [int]$entry }
    else { $procId = [int]$entry.pid }
    return (Test-ProcessAlive $procId)
}

function Stop-TrackedService {
    param([string] $Name)
    $map = Get-PidMap
    if (-not $map.ContainsKey($Name)) { return }
    $entry = $map[$Name]
    $procId = 0; $label = $Name
    if ($entry -is [int] -or $entry -is [long]) { $procId = [int]$entry }
    else {
        $procId = [int]$entry.pid
        if ($entry.label) { $label = [string]$entry.label }
    }
    if ($procId -le 0) { return }
    $proc = Get-ProcessInfo $procId
    if ($proc) {
        Write-Info ("stopping {0} (pid {1}, {2})" -f $label, $procId, $proc.ProcessName)
        Stop-ProcessTree -ProcessId $procId
    }
    $map.Remove($Name)
    Save-PidMap $map
}

function Start-TrackedProcess {
    param(
        [string] $Name,
        [string] $Label,
        [string] $FilePath,
        [string[]] $Arguments,
        [string] $WorkingDirectory,
        [switch] $Visible
    )

    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

    $stdout = Join-Path $LogDir ("{0}.log" -f $Name)
    $stderr = Join-Path $LogDir ("{0}.err.log" -f $Name)

    $startArgs = @{
        FilePath = $FilePath
        WorkingDirectory = $WorkingDirectory
        RedirectStandardOutput = $stdout
        RedirectStandardError = $stderr
        PassThru = $true
    }
    if ($Arguments -and $Arguments.Count -gt 0) { $startArgs['ArgumentList'] = $Arguments }
    if (-not $Visible) { $startArgs['WindowStyle'] = 'Hidden' }

    try {
        $proc = Start-Process @startArgs
    } catch {
        Write-Err ("could not start {0}: {1}" -f $Label, $_.Exception.Message)
        return 0
    }

    $map = Get-PidMap
    $map[$Name] = [pscustomobject]@{ pid = $proc.Id; label = $Label; port = $Ports[$Name] }
    Save-PidMap $map

    Write-Ok ("started {0} (pid {1}) -> logs/{2}.log" -f $Label, $proc.Id, $Name)
    return $proc.Id
}

function Assert-PortFree {
    param([int] $Port, [string] $ServiceName, [switch] $AllowReuse)

    # NB: $PID is a read-only automatic variable, so this must not be named $pid.
    $ownerPid = Get-ListeningPid -Port $Port
    if ($ownerPid -le 0) { return $true }

    $map = Get-PidMap
    $ours = $false
    if ($map.ContainsKey($ServiceName)) {
        $entry = $map[$ServiceName]
        $trackedPid = 0
        if ($entry -is [int] -or $entry -is [long]) { $trackedPid = [int]$entry }
        else { $trackedPid = [int]$entry.pid }
        if ($trackedPid -eq $ownerPid) { $ours = $true }
    }

    $proc = Get-ProcessInfo $ownerPid
    $procName = if ($proc) { $proc.ProcessName } else { 'unknown process' }
    Write-Warn2 ("TCP {0} is already in use by {1} (pid {2})" -f $Port, $procName, $ownerPid)

    if ($ours) {
        Write-Info "that is the $ServiceName this script started earlier - reusing it"
        return $true
    }

    if ($AllowReuse) {
        Write-Warn2 ("stopping the process holding TCP {0}" -f $Port)
        Stop-ProcessTree -ProcessId $ownerPid
        Start-Sleep -Milliseconds 700
        return ((Get-ListeningPid -Port $Port) -le 0)
    }

    $answer = Confirm-Question ("Stop {0} (pid {1}) and continue? This will interrupt whatever it is running" -f $procName, $ownerPid)
    if (-not $answer) {
        Die ("TCP {0} is held by {1} (pid {2}). Stop it, or re-run with -Mode docker, then try again." -f $Port, $procName, $ownerPid)
    }
    Stop-ProcessTree -ProcessId $ownerPid
    Start-Sleep -Milliseconds 700
    if ((Get-ListeningPid -Port $Port) -gt 0) {
        Die ("TCP {0} is still held after killing pid {1}." -f $Port, $ownerPid)
    }
    return $true
}

# --------------------------------------------------------------------------- #
# Step 6a - local mode
# --------------------------------------------------------------------------- #
function Get-DbNameFromUrl {
    param([string] $Url, [string] $Fallback)
    if (-not $Url) { return $Fallback }
    $m = [regex]::Match($Url, '/([^/?]+)(\?.*)?$')
    if ($m.Success) { return $m.Groups[1].Value }
    return $Fallback
}

function Initialize-LocalDatabase {
    $psql = Resolve-PsqlPath
    if (-not $psql) {
        Write-Warn2 'psql.exe not found - skipping the "create database if missing" check'
        return
    }
    $user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { 'postgres' }
    $pass = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { 'postgres' }
    $dbName = Get-DbNameFromUrl -Url $env:DATABASE_URL -Fallback 'rakshak'

    $exists = Invoke-Psql -Psql $psql -Sql "SELECT 1 FROM pg_database WHERE datname='$dbName';" -User $user -Password $pass -DbName 'postgres'
    if ((($exists | Out-String) -match '1')) {
        Write-Ok ("database '{0}' exists" -f $dbName)
        return
    }
    Write-Step ("creating database '{0}'" -f $dbName)
    $created = Invoke-Psql -Psql $psql -Sql "CREATE DATABASE $dbName;" -User $user -Password $pass -DbName 'postgres'
    if ((($created | Out-String) -match 'CREATE DATABASE')) {
        Write-Ok ("database '{0}' created" -f $dbName)
    } else {
        Write-Warn2 ("could not create '{0}': {1}" -f $dbName, (($created | Out-String).Trim()))
    }
}

function Initialize-PythonVenv {
    # Always use the interpreter OUTSIDE .venv to (re)build .venv.
    $basePython = Get-BasePythonCommand
    if (-not $basePython) { Die 'Python is not available (checked again just before venv setup).' }

    $marker = Join-Path $VenvDir '.rakshak-python'
    $stamp = $basePython
    $recordedStamp = ''
    if (Test-Path -LiteralPath $marker) {
        $recordedStamp = (Get-Content -LiteralPath $marker -Raw -ErrorAction SilentlyContinue)
    }

    $needCreate = $false
    $reason = ''
    if ($Rebuild) {
        $needCreate = $true
        $reason = '-Rebuild was requested'
    } elseif (-not (Test-Path -LiteralPath $VenvPython)) {
        $needCreate = $true
        $reason = 'the virtual environment is missing'
    } elseif ($recordedStamp -and ($recordedStamp.Trim() -ne $stamp)) {
        $needCreate = $true
        $reason = ("it was built with a different interpreter ({0})" -f $recordedStamp.Trim())
    }

    if ($needCreate) {
        if ($reason -like '*different interpreter*') {
            Write-Warn2 ("recreating the virtual environment: {0}" -f $reason)
        } else {
            Write-Step ("creating the virtual environment: {0}" -f $reason)
        }
        if (Test-Path -LiteralPath $VenvDir) {
            Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        & $basePython -m venv $VenvDir 2>&1 | Out-Null
        if (-not (Test-Path -LiteralPath $VenvPython)) {
            Die ("Could not create the virtual environment at {0}. Delete the folder and re-run." -f $VenvDir)
        }
        Write-TextFile $marker $stamp
        Write-Ok 'virtual environment ready'
    } else {
        Write-Info 'virtual environment already present'
    }

    $requirementsHash = Get-FileHashHex (Join-Path $BackendDir 'requirements.txt')
    $hashes = Get-StateMap -Path $HashesPath -Property 'hashes'
    $prev = if ($hashes.ContainsKey('backend-requirements')) { [string]$hashes['backend-requirements'] } else { '' }

    if ($Rebuild -or $prev -ne $requirementsHash) {
        Write-Step 'installing backend dependencies (requirements.txt changed)'
        $output = & $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $BackendDir 'requirements.txt') 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-TextFile (Join-Path $LogDir 'pip.log') (($output | Out-String))
            Die ("pip install failed - see logs/pip.log. Common causes: no internet, or a build tool missing for psycopg2/rapidocr.")
        }
        Write-Ok 'backend dependencies installed'
    } else {
        Write-Info 'backend dependencies unchanged - skipping pip install'
    }

    $hashes['backend-requirements'] = $requirementsHash
    Write-JsonFile $HashesPath ([pscustomobject]@{ hashes = $hashes })
}

function Invoke-AlembicUpgrade {
    Write-Step 'applying database migrations (alembic upgrade head)'
    if (-not (Test-Path -LiteralPath $VenvPython)) { Die 'the virtual environment is missing - re-run start.' }
    # alembic reads DATABASE_URL from the process environment, which was imported
    # from .env by Initialize-EnvFiles - so it targets the same DB as the API.
    # The console script is used because `python -m alembic` is not supported.
    $alembicExe = Join-Path $VenvDir 'Scripts\alembic.exe'
    if (-not (Test-Path -LiteralPath $alembicExe)) {
        Die 'alembic is not installed in .venv - re-run with -Rebuild.'
    }
    Push-Location $BackendDir
    try {
        $out = & $alembicExe upgrade head 2>&1
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        Write-TextFile (Join-Path $LogDir 'alembic.log') (($out | Out-String))
        Die 'alembic upgrade head failed - see logs/alembic.log'
    }
    Write-Ok 'database schema is up to date'
}

function Get-NodeLauncher {
    <#
        Returns a hashtable with FilePath/Arguments that starts a JS CLI inside
        $Dir. Launching `node <pkg>/bin/...` directly avoids Start-Process and
        .cmd quoting problems on Windows (paths with spaces) and makes the PID we
        track the real process instead of a cmd.exe wrapper.
    #>
    param([string] $Dir, [string] $RelativeJs, [string[]] $CliArgs)

    $jsPath = Join-Path (Join-Path $Dir 'node_modules') $RelativeJs
    if (Test-Path -LiteralPath $jsPath) {
        $node = Test-Command 'node'
        if ($node) {
            $all = @($jsPath) + $CliArgs
            return @{ FilePath = $node; Arguments = $all }
        }
    }

    $npx = Test-Command 'npx'
    if ($npx) {
        $pkgName = $RelativeJs.Split('/')[0]
        $all = @($pkgName) + $CliArgs
        return @{ FilePath = $npx; Arguments = $all }
    }
    return $null
}

function Start-BackendLocal {
    # FastAPI reads settings (CORS_ORIGINS above all) once, at import time, and
    # a reused uvicorn process never re-reads .env. Initialize-EnvFiles has
    # already rewritten .env by this point, so if the file changed since that
    # process started, the live backend is still serving the previous CORS list
    # and every request from the LAN origin is blocked by the browser.
    $hashes = Get-StateMap -Path $HashesPath -Property 'hashes'
    $envHash = Get-CombinedHash @($EnvPath)

    if (Test-ServiceRunning -Name 'backend' -and (Get-ListeningPid -Port $Ports.backend) -gt 0) {
        $prevEnvHash = if ($hashes.ContainsKey('backend_env')) { [string]$hashes['backend_env'] } else { '' }
        if ($prevEnvHash -eq $envHash) {
            Write-Ok 'backend already running (reusing it)'
            $script:Fresh = $false
            return
        }

        Write-Warn2 '.env changed since the backend started - restarting it so it picks up the new CORS_ORIGINS'
        Stop-TrackedService -Name 'backend'
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline -and (Get-ListeningPid -Port $Ports.backend) -gt 0) {
            Start-Sleep -Milliseconds 250
        }
    }

    Assert-PortFree -Port $Ports.backend -ServiceName 'backend' | Out-Null
    Start-TrackedProcess -Name 'backend' -Label 'FastAPI backend' `
        -FilePath $VenvPython `
        -Arguments @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', "$($Ports.backend)") `
        -WorkingDirectory $BackendDir | Out-Null

    # Remember which .env this process was started with, for the check above.
    $hashes['backend_env'] = $envHash
    Write-JsonFile $HashesPath ([pscustomobject]@{ hashes = $hashes })
}

function Initialize-NodeDeps {
    param([string] $Dir, [string] $Key, [switch] $AllowInstall)

    $lock = Join-Path $Dir 'package-lock.json'
    $modules = Join-Path $Dir 'node_modules'
    $hashes = Get-StateMap -Path $HashesPath -Property 'hashes'
    $prev = if ($hashes.ContainsKey($Key)) { [string]$hashes[$Key] } else { '' }
    $current = Get-CombinedHash @($lock, (Join-Path $Dir 'package.json'))

    $needInstall = $Rebuild -or $prev -ne $current -or -not (Test-Path -LiteralPath $modules)
    if (-not $needInstall) {
        Write-Info ("{0}: dependencies unchanged - skipping npm ci" -f (Split-Path -Leaf $Dir))
        $hashes[$Key] = $current
        Write-JsonFile $HashesPath ([pscustomobject]@{ hashes = $hashes })
        return
    }

    if (-not $AllowInstall) {
        Write-Info ("{0}: dependencies need installing (skipped)" -f (Split-Path -Leaf $Dir))
        return
    }

    Write-Step ("{0}: installing node dependencies" -f (Split-Path -Leaf $Dir))
    $npm = Test-Command 'npm'
    if (-not $npm) { Die 'npm was not found on PATH.' }
    Push-Location $Dir
    try {
        & $npm ci --no-audit --no-fund 2>&1 | Out-Null
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        Die ("npm ci failed in {0}. Delete node_modules there and re-run, or check your internet connection." -f $Dir)
    }
    Write-Ok ("{0}: node dependencies installed" -f (Split-Path -Leaf $Dir))
    $hashes[$Key] = $current
    Write-JsonFile $HashesPath ([pscustomobject]@{ hashes = $hashes })
}

function Start-FrontendLocal {
    param([string] $Name, [string] $Dir, [int] $Port, [string] $Label, [switch] $AllowInstall)

    if (Test-ServiceRunning -Name $Name -and (Get-ListeningPid -Port $Port) -gt 0) {
        Write-Ok ("{0} already running (reusing it)" -f $Label)
        $script:Fresh = $false
        return
    }
    Assert-PortFree -Port $Port -ServiceName $Name | Out-Null
    Initialize-NodeDeps -Dir $Dir -Key ("{0}-lock" -f $Name) -AllowInstall:$AllowInstall | Out-Null

    $launcher = Get-NodeLauncher -Dir $Dir -RelativeJs 'vite/bin/vite.js' -CliArgs @('--host', '0.0.0.0', '--port', "$Port")
    if ($null -eq $launcher) {
        Write-Warn2 ("{0}: vite is not installed yet (no node_modules) - skipping this app" -f $Label)
        Write-Warn2 ("run `npm ci` in {0} then re-run start" -f $Dir)
        return
    }
    Start-TrackedProcess -Name $Name -Label $Label `
        -FilePath $launcher.FilePath `
        -Arguments $launcher.Arguments `
        -WorkingDirectory $Dir | Out-Null
}

function Start-BackendDocker {
    Write-Step 'docker compose up -d'
    $composeArgs = @('compose', 'up', '-d')
    if ($Rebuild) { $composeArgs += '--build' }

    $composeHash = Get-CombinedHash @(
        (Join-Path $RepoRoot 'docker-compose.yml'),
        (Join-Path $BackendDir 'Dockerfile'),
        (Join-Path $BackendDir 'requirements.txt'),
        (Join-Path $CitizenDir 'Dockerfile'),
        (Join-Path $CitizenDir 'package.json'),
        (Join-Path $CitizenDir 'package-lock.json'),
        (Join-Path $PoliceDir 'Dockerfile'),
        (Join-Path $PoliceDir 'package.json'),
        (Join-Path $PoliceDir 'package-lock.json')
    )
    $hashes = Get-StateMap -Path $HashesPath -Property 'hashes'
    $prev = if ($hashes.ContainsKey('docker-build')) { [string]$hashes['docker-build'] } else { '' }
    if (-not $Rebuild -and $prev -ne $composeHash -and $prev -ne '') {
        Write-Info 'Dockerfiles / requirements / package.json changed - adding --build'
        $composeArgs += '--build'
    }

    Push-Location $RepoRoot
    try {
        $out = & docker @composeArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $log = (($out | Out-String))
    if ($code -ne 0) {
        Write-TextFile (Join-Path $LogDir 'docker.log') $log
        Die ("docker compose up failed (exit $code) - see logs/docker.log")
    }
    $hashes['docker-build'] = $composeHash
    Write-JsonFile $HashesPath ([pscustomobject]@{ hashes = $hashes })
    Write-Ok 'containers started'

    Write-Step 'waiting for containers to report healthy'
    Push-Location $RepoRoot
    try {
        & docker compose ps 2>&1 | Out-String | Write-Info
    } finally {
        Pop-Location
    }
}

function Invoke-DockerSeed {
    Write-Step 'seeding demo data inside the backend container'
    Push-Location $RepoRoot
    try {
        & docker compose exec -T backend python scripts/seed_demo.py 2>&1 | Out-String | Write-Info
    } finally {
        Pop-Location
    }
}

# --------------------------------------------------------------------------- #
# Step 8 - seeding
# --------------------------------------------------------------------------- #
function Invoke-Seeding {
    param([switch] $Force)

    $seedScript = Join-Path $RepoRoot 'scripts\seed_demo.py'
    $seedHash = Get-FileHashHex $seedScript

    if (-not $Force -and -not $ForceSeed -and (Test-Path -LiteralPath $SeedMarkerPath)) {
        $marker = (Get-Content -LiteralPath $SeedMarkerPath -Raw -ErrorAction SilentlyContinue).Trim()
        if ($marker -eq $seedHash) {
            Write-Ok 'demo data already seeded (skipping - idempotent, runs once)'
            Write-Info 'to re-seed: .\start.bat -Reset  (or -ForceSeed)'
            return
        }
    }

    Write-Step 'seeding demo users and demo data (idempotent)'
    if (-not (Test-Path -LiteralPath $seedScript)) {
        Write-Warn2 'scripts/seed_demo.py not found - demo users are still seeded by the backend on startup'
        return
    }

    if ($script:EffectiveMode -eq 'docker') {
        Invoke-DockerSeed
        Write-TextFile $SeedMarkerPath $seedHash
        return
    }

    $python = Get-PythonCommand
    if (-not $python) { return }
    Push-Location $RepoRoot
    try {
        $out = & $python (Join-Path $RepoRoot 'scripts\seed_demo.py') 2>&1
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $text = ($out | Out-String).Trim()
    if ($code -ne 0) {
        Write-Warn2 ("demo seeding exited with code {0}" -f $code)
        Write-Warn2 $text
        Write-Warn2 'the demo still works - users are seeded by the backend on startup'
        return
    }
    Write-TextFile (Join-Path $LogDir 'seed_demo.log') $text
    Write-Ok 'demo data seeded (see logs/seed_demo.log)'
    Write-TextFile $SeedMarkerPath $seedHash
}

# --------------------------------------------------------------------------- #
# Step 9 - Expo (always on the host)
# --------------------------------------------------------------------------- #
function Resolve-Adb {
    <#
        adb is not always on PATH. Look in the three places that matter, and if
        none of them has it, fetch Google's platform-tools into .rakshak so no
        admin rights or Android Studio install are needed.
    #>
    $candidates = @()
    $onPath = Test-Command 'adb'
    if ($onPath) { $candidates += $onPath }

    $local = Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'
    if (Test-Path -LiteralPath $local) { $candidates += $local }

    $vendored = Join-Path $StateDir 'platform-tools\adb.exe'
    if (Test-Path -LiteralPath $vendored) { $candidates += $vendored }

    if ($candidates.Count -gt 0) { return @($candidates)[0] }

    if ($script:AdbDownloaded) { return $null }
    $script:AdbDownloaded = $true

    Write-Step 'adb not found - downloading Google platform-tools (one time, no admin needed)'
    $zip = Join-Path $StateDir 'platform-tools.zip'
    try {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' `
            -OutFile $zip -UseBasicParsing -TimeoutSec 180
        Expand-Archive -LiteralPath $zip -DestinationPath $StateDir -Force
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Warn2 ("could not download adb: {0}" -f $_.Exception.Message)
        return $null
    }

    if (Test-Path -LiteralPath $vendored) {
        Write-Ok 'adb installed into .rakshak\platform-tools'
        return $vendored
    }
    return $null
}

function Get-PhoneConnection {
    <#
        Returns 'authorized', 'unauthorized' or 'none'. Never throws: a phone
        being absent is the normal case, not a failure.
    #>
    $adb = Resolve-Adb
    if (-not $adb) { return 'no-adb' }

    $out = ''
    try {
        $out = (& $adb devices 2>&1 | Out-String)
    } catch {
        return 'no-adb'
    }

    if ($out -match 'unauthorized') { return 'unauthorized' }
    if ($out -match '(?m)^\s*device\s+\S+') { return 'authorized' }
    return 'none'
}

function Sync-ExpoSdk {
    <#
        Keep the native modules on the SDK the installed `expo` expects, then
        report doctor failures only - a wall of passing checks is noise in a
        start script.
    #>
    $launcher = Get-NodeLauncher -Dir $MobileDir -RelativeJs 'expo/bin/cli' -CliArgs @('--version')
    if ($null -eq $launcher) { return }

    Push-Location $MobileDir
    try {
        $check = & $launcher.FilePath @($launcher.Arguments + @('install', '--check')) 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or $check -match 'should be updated|expected version') {
            Write-Step 'mobile_app: native modules do not match this SDK - running expo install --fix'
            & $launcher.FilePath @($launcher.Arguments + @('install', '--fix')) 2>&1 | Out-Null
            Write-Ok 'mobile_app: native modules realigned to the installed Expo SDK'
        }

        # SDK 58 removed the `expo doctor` subcommand ("not supported in the
        # local CLI, please use npx expo-doctor instead"), so the standalone
        # binary is the only way to run the checks.
        $doctorBin = Join-Path $MobileDir 'node_modules\.bin\expo-doctor.cmd'
        if (-not (Test-Path -LiteralPath $doctorBin)) {
            $doctorBin = Join-Path $MobileDir 'node_modules\.bin\expo-doctor'
        }
        if (-not (Test-Path -LiteralPath $doctorBin)) {
            Write-Warn2 'mobile_app: expo-doctor is not installed - run: npx expo install expo-doctor -- --save-dev'
            return
        }

        $doctor = & $doctorBin 2>&1 | Out-String
        if ($doctor -match '(\d+)/(\d+) checks passed') {
            $passed = $Matches[1]; $total = $Matches[2]
            if ([int]$passed -eq [int]$total) {
                Write-Ok ("mobile_app: expo-doctor {0}/{1} passed" -f $passed, $total)
            } else {
                Write-Warn2 ("mobile_app: expo-doctor {0}/{1} passed" -f $passed, $total)
                foreach ($line in ($doctor -split "`r?`n")) {
                    if ($line -match '^\s*(✖|Advice:|Install |Missing |npx expo )') {
                        Write-Warn2 ("    " + $line.Trim())
                    }
                }
            }
        } else {
            Write-Warn2 'mobile_app: expo-doctor produced no summary line'
        }
    } catch {
        Write-Warn2 ("mobile_app: expo check failed: {0}" -f $_.Exception.Message)
    } finally {
        Pop-Location
    }
}

function Start-Expo {
    if ($NoMobile) {
        Write-Info 'mobile app skipped (-NoMobile)'
        return
    }

    if ((Get-ListeningPid -Port $Ports.expo) -gt 0) {
        Write-Ok 'Expo Metro already running (reusing it)'
        $script:Fresh = $false
        $script:ExpoRunning = $true
        return
    }

    Initialize-NodeDeps -Dir $MobileDir -Key 'mobile-lock' -AllowInstall | Out-Null
    Sync-ExpoSdk
    Assert-PortFree -Port $Ports.expo -ServiceName 'expo' | Out-Null

    # --clear whenever something that shapes the bundle changed since the last
    # run: package.json, app.json or the API URL. A stale Metro cache is the
    # classic "my edit did nothing" cause. Hash-checked rather than always on so
    # repeat start-ups stay quick.
    $hashes = Get-StateMap -Path $HashesPath -Property 'hashes'
    $configHash = Get-CombinedHash @(
        (Join-Path $MobileDir 'package.json'),
        (Join-Path $MobileDir 'app.json'),
        (Join-Path $MobileDir '.env')
    )
    $prevConfig = if ($hashes.ContainsKey('expo-config')) { [string]$hashes['expo-config'] } else { '' }
    $configChanged = ($prevConfig -ne $configHash)
    $hashes['expo-config'] = $configHash
    Write-JsonFile $HashesPath ([pscustomobject]@{ hashes = $hashes })

    # Metro has to run on the HOST: a container breaks LAN/QR discovery, which is
    # exactly how the phone finds the demo server.
    $cliArgs = @('start', '--lan')
    if ($Rebuild -or $configChanged) {
        $cliArgs += '--clear'
        Write-Info 'mobile_app: config changed since last run - starting Metro with --clear'
    }

    # A USB-attached phone lets the CLI install/open the matching Expo Go on it.
    # Without one the QR path is used and the phone needs no cable at all.
    $script:PhoneState = Get-PhoneConnection
    if ($script:PhoneState -eq 'authorized') {
        Write-Ok 'phone detected over USB - Expo CLI will open the app on it'
        $cliArgs += '--android'
    } elseif ($script:PhoneState -eq 'unauthorized') {
        Write-Warn2 'phone seen but not authorised - tap "Allow USB debugging" on it, then re-run start.bat'
    }
    $launcher = Get-NodeLauncher -Dir $MobileDir -RelativeJs 'expo/bin/cli' -CliArgs $cliArgs
    if ($null -eq $launcher) {
        Write-Warn2 'expo CLI not installed - start Metro manually:  cd mobile_app; npx expo start --lan'
        return
    }

    # The user needs to SEE the QR code, so this runs in a real console window.
    # It is launched through `cmd /c start` on purpose: `start` gives the child a
    # brand new console and detaches it from this script's stdout, so a piped or
    # redirected start.ps1 call returns instead of hanging on the Metro process.
    Write-Step 'starting Expo Metro on the host (a new window opens with the QR code)'
    $inner = '"' + $launcher.FilePath + '" ' + (($launcher.Arguments | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' ')
    $inner = 'cd /d "' + $MobileDir + '" && ' + $inner

    try {
        Start-Process -FilePath $env:ComSpec `
            -ArgumentList @('/c', 'start', '"RAKSHAK - Expo Metro"', $env:ComSpec, '/k', $inner) `
            -WorkingDirectory $MobileDir | Out-Null
    } catch {
        Write-Warn2 ("could not open the Expo window: {0}" -f $_.Exception.Message)
        return
    }

    # Track whatever ends up owning TCP 8081 so stop.ps1 can kill it. Resolved
    # after Metro binds the port, because `start` returns before that.
    Write-Ok 'Expo Metro starting - look for the QR code in the new window'
    Write-Info '  (press r to reload, a for Android, or scan the QR with Expo Go)'
    $script:ExpoPending = $true
}

function Resolve-ExpoPid {
    <#
        `cmd /c start` does not hand back the Metro PID, so once the port is
        bound we record its owner. Stopping that tree (taskkill /T) closes the
        Expo window and Metro together.
    #>
    if (-not $script:ExpoPending) { return }
    $script:ExpoPending = $false

    $deadline = (Get-Date).AddSeconds(90)
    $owner = 0
    while ((Get-Date) -lt $deadline -and $owner -le 0) {
        $owner = Get-ListeningPid -Port $Ports.expo
        if ($owner -le 0) { Start-Sleep -Seconds 2 }
    }
    if ($owner -le 0) {
        Write-Warn2 'Expo Metro did not answer on TCP 8081 - check the new window for an error'
        return
    }

    # Metro is a child of the console window that `start` opened. Tracking that
    # top-most cmd.exe means stop.ps1 closes the window as well as Metro.
    $target = $owner
    $guard = 0
    while ($guard -lt 6) {
        $guard++
        $info = $null
        try {
            $info = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $target) -ErrorAction SilentlyContinue
        } catch { $info = $null }
        if ($null -eq $info) { break }
        $parentId = [int]$info.ParentProcessId
        if ($parentId -le 0) { break }
        $parent = $null
        try {
            $parent = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $parentId) -ErrorAction SilentlyContinue
        } catch { $parent = $null }
        if ($null -eq $parent) { break }
        if ($parent.Name -ne 'cmd.exe') { break }
        $target = $parentId
    }

    $map = Get-PidMap
    $map['expo'] = [pscustomobject]@{ pid = $target; label = 'Expo Metro'; port = $Ports.expo }
    Save-PidMap $map
    Write-Ok ("Expo Metro is listening on 8081 (pid {0}) - tracked for stop" -f $target)
}

# --------------------------------------------------------------------------- #
# Step 7 + 10 - readiness + summary
# --------------------------------------------------------------------------- #
function Test-Readiness {
    $results = @()

    $backendUrl = "http://127.0.0.1:$($Ports.backend)/api/v1/healthz"
    $backendRoot = "http://127.0.0.1:$($Ports.backend)/health"
    $ok = Wait-ForHttp -Url $backendUrl -TimeoutSec 60 -Label 'backend'
    if (-not $ok) { $ok = Wait-ForHttp -Url $backendRoot -TimeoutSec 5 -Label 'backend' }
    $results += [pscustomobject]@{ Service = 'Backend API'; Ok = $ok; Note = if ($ok) { "/docs" } else { 'logs/backend.err.log' } }

    $citizenOk = Wait-ForHttp -Url "http://127.0.0.1:$($Ports.citizen)/" -TimeoutSec 30 -Label 'citizen'
    $results += [pscustomobject]@{ Service = 'Citizen web'; Ok = $citizenOk; Note = if ($citizenOk) { '' } else { 'logs/citizen.err.log' } }

    $policeOk = Wait-ForHttp -Url "http://127.0.0.1:$($Ports.police)/" -TimeoutSec 30 -Label 'police'
    $results += [pscustomobject]@{ Service = 'Police dashboard'; Ok = $policeOk; Note = if ($policeOk) { '' } else { 'logs/police.err.log' } }

    if ($NoMobile) {
        Write-Info 'Expo Metro is started at the end (after this check) - see the summary'
    }

    $redisOk = $false
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and -not $redisOk) {
        $redisOk = Test-RedisReachable
        if (-not $redisOk) { Start-Sleep -Milliseconds 500 }
    }
    $results += [pscustomobject]@{ Service = 'Redis'; Ok = $redisOk; Note = if ($script:EffectiveMode -eq 'docker') { 'redis:6379' } else { 'localhost:6379' } }

    $pgOk = $false
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and -not $pgOk) {
        $pgOk = Test-PostgresReachable
        if (-not $pgOk) { Start-Sleep -Milliseconds 500 }
    }
    $results += [pscustomobject]@{ Service = 'PostgreSQL'; Ok = $pgOk; Note = 'localhost:5432' }

    return $results
}

function Test-ExpoReadiness {
    <#
        Expo is started in step 9, so its readiness is checked right after that
        (not in Test-Readiness, which runs before Metro exists).
    #>
    if ($NoMobile) { return @() }

    $ok = Wait-ForHttp -Url "http://127.0.0.1:$($Ports.expo)/status" -TimeoutSec 60 -Label 'expo'
    if (-not $ok) {
        # A reused Metro can be listening before /status answers; the port is
        # what the phone actually connects to.
        $ok = ((Get-ListeningPid -Port $Ports.expo) -gt 0)
    }
    if ($ok) { Write-Ok 'Expo Metro' } else { Write-Err 'Expo Metro  (see the Expo window / logs/expo.err.log)' }
    return @([pscustomobject]@{ Service = 'Expo Metro'; Ok = $ok; Note = if ($ok) { 'QR in the new window' } else { 'logs/expo.err.log' } })
}

function Write-SummaryTable {
    param($Results, [string] $Ip)

    $lan = "http://${Ip}"
    $rows = @(
        @{ Service = 'Backend API';       Lan = "${lan}:8000/docs";       Local = 'http://localhost:8000/docs'; Status = $null },
        @{ Service = 'Citizen web';       Lan = "${lan}:5173";             Local = 'http://localhost:5173';     Status = $null },
        @{ Service = 'Police dashboard';  Lan = "${lan}:5174";             Local = 'http://localhost:5174';     Status = $null },
        @{ Service = 'Expo Metro';        Lan = "${lan}:8081";             Local = 'http://localhost:8081';     Status = $null },
        @{ Service = 'PostgreSQL';        Lan = 'localhost:5432';          Local = 'localhost:5432';            Status = $null },
        @{ Service = 'Redis';             Lan = 'localhost:6379';          Local = 'localhost:6379';            Status = $null },
        @{ Service = 'Phone over USB';    Lan = '-';                       Local = '-';                         Status = $null }
    )

    foreach ($row in $rows) {
        $hit = $null
        foreach ($res in $Results) {
            if ($res.Service -eq $row.Service) { $hit = $res }
        }
        if ($null -ne $hit) {
            $row.Status = if ($hit.Ok) { 'PASS' } else { 'FAIL' }
        } elseif ($row.Service -eq 'Expo Metro' -and $NoMobile) {
            $row.Status = 'SKIPPED'
        } elseif ($row.Service -eq 'Phone over USB') {
            $row.Status = switch ($script:PhoneState) {
                'authorized'   { 'CONNECTED' }
                'unauthorized' { 'TAP ALLOW' }
                'no-adb'       { 'NO ADB' }
                'not-checked'  { 'SKIPPED' }
                default        { 'NOT PLUGGED' }
            }
        } else {
            $row.Status = 'n/a'
        }
    }

    $w1 = 0; $w2 = 0; $w3 = 0
    foreach ($r in $rows) {
        if ($r.Service.Length -gt $w1) { $w1 = $r.Service.Length }
        if ($r.Lan.Length -gt $w2) { $w2 = $r.Lan.Length }
        if ($r.Status.Length -gt $w3) { $w3 = $r.Status.Length }
    }

    Write-Host ''
    Write-Host ('  {0}  {1}  {2}  {3}' -f 'SERVICE'.PadRight($w1), 'URL ON THIS NETWORK'.PadRight($w2), 'LOCAL'.PadRight(28), 'STATUS') -ForegroundColor DarkGray
    Write-Host ('  ' + ('-' * ($w1 + $w2 + 46))) -ForegroundColor DarkGray
    foreach ($r in $rows) {
        $color = 'Gray'
        if ($r.Status -eq 'PASS') { $color = 'Green' }
        elseif ($r.Status -eq 'FAIL') { $color = 'Red' }
        elseif ($r.Status -eq 'SKIPPED') { $color = 'Yellow' }
        $line = '  {0}  {1}  {2}  {3}' -f $r.Service.PadRight($w1), $r.Lan.PadRight($w2), $r.Local.PadRight(28), $r.Status
        Write-Host $line -ForegroundColor $color
    }
}

function Write-ServiceFailureLogs {
    <#
        A Vite build error or a port clash is invisible from the summary table:
        the service just shows FAIL. Dump the tail of its log so the exact
        red line lands on screen instead of staying in logs\<service>.err.log.
    #>
    param($Results)

    $logFor = @{
        'Backend API'      = 'backend'
        'Citizen web'      = 'citizen'
        'Police dashboard' = 'police'
        'Expo Metro'       = 'expo'
    }

    $anyFailure = $false
    foreach ($res in @($Results)) {
        if ($res.Ok) { continue }
        $key = $logFor[$res.Service]
        if (-not $key) { continue }
        $anyFailure = $true

        Write-Host ''
        Write-Host ("  !! {0} did not come up - last 20 log lines" -f $res.Service) -ForegroundColor Red

        foreach ($suffix in @('err.log', 'log')) {
            $path = Join-Path $LogDir ("{0}.{1}" -f $key, $suffix)
            if (-not (Test-Path -LiteralPath $path)) { continue }
            $lines = @(Get-Content -LiteralPath $path -Tail 20 -ErrorAction SilentlyContinue |
                Where-Object { $_.Trim() -ne '' })
            if ($lines.Count -eq 0) { continue }
            Write-Host ("     --- {0} ---" -f (Split-Path -Leaf $path)) -ForegroundColor DarkGray
            foreach ($line in $lines) { Write-Host ('     ' + $line) -ForegroundColor DarkYellow }
        }
    }

    if ($anyFailure) {
        Write-Host ''
        Write-Host '  Fix the line above, then re-run start.bat.' -ForegroundColor Yellow
    }
}

function Write-FinalSummary {
    param($Results, [string] $Ip)

    Write-Banner 'RAKSHAK is up' Green
    Write-SummaryTable -Results $Results -Ip $Ip
    Write-ServiceFailureLogs -Results $Results

    Write-Host ''
    Write-Host '  Swagger API docs : ' -NoNewline -ForegroundColor DarkGray
    Write-Host "http://${Ip}:8000/docs" -ForegroundColor Cyan
    Write-Host '  Backend health   : ' -NoNewline -ForegroundColor DarkGray
    Write-Host "http://${Ip}:8000/health" -ForegroundColor Cyan

    Write-Host ''
    Write-Host '  Mobile app' -ForegroundColor Yellow
    $expoSdk = 'unknown'
    try {
        $pkgPath = Join-Path $MobileDir 'package.json'
        if (Test-Path -LiteralPath $pkgPath) {
            $expoSdk = [string]((Get-Content -LiteralPath $pkgPath -Raw | ConvertFrom-Json).dependencies.expo)
        }
    } catch { $expoSdk = 'unknown' }
    Write-Host '    Expo SDK   : ' -NoNewline -ForegroundColor DarkGray
    Write-Host $expoSdk -ForegroundColor Cyan
    Write-Host '    Expo URL   : ' -NoNewline -ForegroundColor DarkGray
    Write-Host "exp://${Ip}:8081" -ForegroundColor Cyan
    Write-Host '    API target : ' -NoNewline -ForegroundColor DarkGray
    $apiUrl = 'not set'
    try {
        $mobileEnv = Join-Path $MobileDir '.env'
        if (Test-Path -LiteralPath $mobileEnv) {
            $line = Select-String -LiteralPath $mobileEnv -Pattern 'EXPO_PUBLIC_API_URL' -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($line) { $apiUrl = ($line.Line -split '=', 2)[1].Trim() }
        }
    } catch { $apiUrl = 'not set' }
    Write-Host $apiUrl -ForegroundColor Cyan

    if (-not $NoMobile) {
        # Exactly one line, and only when it is actually needed.
        if ($script:PhoneState -eq 'authorized') {
            Write-Host '    Phone      : ' -NoNewline -ForegroundColor DarkGray
            Write-Host 'connected over USB - Expo CLI opens the app for you' -ForegroundColor Green
        } else {
            Write-Host ''
            Write-Host '  To install Expo Go SDK 58: plug the phone in by USB, enable USB debugging,' -ForegroundColor Yellow
            Write-Host '  tap Allow, then run start.bat again (only needed once).' -ForegroundColor Yellow
        }
    }

    Write-Host ''
    Write-Host '  Demo logins' -ForegroundColor Yellow
    Write-Host '    citizen    citizen@example.com    Citizen@123'
    Write-Host '    volunteer  volunteer@example.com  Volunteer@123'
    Write-Host '    cop        cop@example.com        Police@123'
    Write-Host '    admin      admin@example.com      Admin@123'

    Write-Host ''
    Write-Host '  On your phone: same Wi-Fi as this PC, open Expo Go / the dev client and scan the QR' -ForegroundColor Green

    if ($script:Fresh) {
        Write-Host ''
        Write-Host '  Starting order used: postgres, redis, backend, citizen, police, then Expo.' -ForegroundColor DarkGray
    } else {
        Write-Host ''
        Write-Host '  Some services were already running and were reused (no duplicates created).' -ForegroundColor DarkGray
    }

    if (-not $NoMobile) {
        Write-Host ''
        Write-Host '  Expo window: press r to reload, a for Android, or scan the QR with Expo Go.' -ForegroundColor DarkGray
    }

    Write-Host ''
    Write-Host '  Logs    : logs\<service>.log   (e.g. logs\backend.log)' -ForegroundColor DarkGray
    Write-Host '  Stop    : .\stop.bat' -ForegroundColor DarkGray
    Write-Host '  Restart : .\start.bat   (safe to run twice)' -ForegroundColor DarkGray
    Write-Host ''
}

# --------------------------------------------------------------------------- #
# Reset
# --------------------------------------------------------------------------- #
function Invoke-Reset {
    param([string] $InMode)

    Write-Banner 'RESET' Yellow
    Write-Warn2 'This will stop every service AND delete the demo data.'
    if ($InMode -eq 'docker') {
        Write-Warn2 '  docker volumes (postgres data) are wiped'
    } else {
        Write-Warn2 ("  the PostgreSQL database '{0}' is dropped and recreated" -f $(if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { 'rakshak' }))
    }
    if (-not (Confirm-Question 'Are you sure?')) {
        Write-Info 'reset cancelled'
        return
    }

    Write-Step 'stopping everything'
    $stopScript = Join-Path $ScriptDir 'stop.ps1'
    if (Test-Path -LiteralPath $stopScript) {
        & $stopScript -Mode $InMode | Out-Null
    }

    if ($InMode -eq 'docker') {
        Write-Step 'removing containers and volumes'
        Push-Location $RepoRoot
        try { & docker compose down -v 2>&1 | Out-String | Write-Info } finally { Pop-Location }
        Write-Ok 'docker volumes removed'
        Remove-Item -LiteralPath $SeedMarkerPath -Force -ErrorAction SilentlyContinue
        return
    }

    $psql = Resolve-PsqlPath
    $dbName = if ($env:DATABASE_URL) { Get-DbNameFromUrl -Url $env:DATABASE_URL -Fallback 'rakshak' } else { 'rakshak' }
    if ($psql) {
        $user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { 'postgres' }
        $pass = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { 'postgres' }
        Write-Step ("dropping database '{0}'" -f $dbName)
        Invoke-Psql -Psql $psql -Sql "DROP DATABASE IF EXISTS ${dbName} WITH (FORCE);" -User $user -Password $pass -DbName 'postgres' | Out-Null
        Invoke-Psql -Psql $psql -Sql "CREATE DATABASE ${dbName};" -User $user -Password $pass -DbName 'postgres' | Out-Null
        Write-Ok ("database '{0}' dropped and recreated" -f $dbName)
    } else {
        Write-Warn2 'psql.exe not found - drop the database manually:  DROP DATABASE rakshak WITH (FORCE); CREATE DATABASE rakshak;'
    }

    foreach ($service in @('backend', 'citizen', 'police', 'expo')) {
        Stop-TrackedService -Name $service
    }
    foreach ($port in $Ports.Values) {
        $owner = Get-ListeningPid -Port $port
        if ($owner -gt 0) {
            Write-Warn2 ("TCP {0} still held by pid {1} - stopping it" -f $port, $owner)
            Stop-ProcessTree -ProcessId $owner
        }
    }
    Remove-Item -LiteralPath $SeedMarkerPath -Force -ErrorAction SilentlyContinue
    Write-Ok 'reset finished - the services will start again in a moment'
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
$exitCode = 0
try {
    Write-Banner 'RAKSHAK - one command startup' Cyan
    Write-Info ("repository : {0}" -f $RepoRoot)

    # --- Step 2 first: the IP decides every URL we are about to write --------- #
    $script:LanIp = Get-LanIpv4 -Override $HostIp
    Write-Step ("LAN IP    : {0}" -f $script:LanIp)

    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

    # --- mode selection ------------------------------------------------------ #
    $dockerAvailable = $false
    $dockerCli = Test-Command 'docker'
    if ($dockerCli) {
        & $dockerCli info *> $null
        if ($LASTEXITCODE -eq 0) { $dockerAvailable = $true }
    }

    if ($Mode -eq 'auto') {
        if ($dockerAvailable) {
            $script:EffectiveMode = 'docker'
        } else {
            $script:EffectiveMode = 'local'
            if ($dockerCli) {
                Write-Warn2 'the Docker CLI is installed but the daemon is not running - falling back to local mode'
                Write-Warn2 'start Docker Desktop and re-run if you prefer containers:  docker info'
            }
        }
    } elseif ($Mode -eq 'docker') {
        if (-not $dockerCli) { Die 'Docker mode requested but the "docker" command was not found on PATH.' }
        if (-not $dockerAvailable) {
            Write-Warn2 'the Docker daemon is not running (docker info failed)'
            Write-Warn2 'falling back to local mode; start Docker Desktop to use containers'
            $script:EffectiveMode = 'local'
        } else {
            $script:EffectiveMode = 'docker'
        }
    } else {
        $script:EffectiveMode = 'local'
    }

    Write-Step ("mode      : {0}{1}" -f $script:EffectiveMode.ToUpper(), $(if ($Rebuild) { ' (rebuild)' } else { '' }))

    # --- Step 1 ------------------------------------------------------------- #
    Assert-Prerequisites -InMode $script:EffectiveMode

    # --- Step 3 + 4 ---------------------------------------------------------- #
    Initialize-EnvFiles -Ip $script:LanIp

    # --- Step 5 ------------------------------------------------------------- #
    Initialize-Firewall -Ip $script:LanIp

    # --- Reset --------------------------------------------------------------- #
    if ($Reset) {
        Invoke-Reset -InMode $script:EffectiveMode
        # Re-read .env into the environment (reset may have touched it).
        Import-DotEnv -Path $EnvPath
    }

    # --- Step 6 -------------------------------------------------------------- #
    if ($script:EffectiveMode -eq 'docker') {
        Start-BackendDocker
    } else {
        Initialize-LocalDatabase
        Initialize-PythonVenv
        Invoke-AlembicUpgrade
        Start-BackendLocal
        Start-FrontendLocal -Name 'citizen' -Dir $CitizenDir -Port $Ports.citizen -Label 'citizen web' -AllowInstall
        Start-FrontendLocal -Name 'police'  -Dir $PoliceDir  -Port $Ports.police  -Label 'police dashboard' -AllowInstall
    }

    # --- Step 7 -------------------------------------------------------------- #
    Write-Banner 'Waiting for services' Cyan
    $results = Test-Readiness
    foreach ($res in $results) {
        if ($res.Ok) { Write-Ok $res.Service }
        else {
            Write-Err ("{0}  ({1})" -f $res.Service, $res.Note)
            $exitCode = 1
        }
    }

    # --- Step 8 -------------------------------------------------------------- #
    $backendUp = ($results | Where-Object { $_.Service -eq 'Backend API' }).Ok
    if ($backendUp) {
        Invoke-Seeding
    } else {
        Write-Warn2 'backend is not healthy - skipping demo seeding'
    }

    # --- Step 9 -------------------------------------------------------------- #
    Start-Expo
    Resolve-ExpoPid

    # Expo readiness is checked here (it did not exist during Test-Readiness).
    # @() is load-bearing: a one-element array returned from a function is
    # unrolled to a bare object, and a bare object's .Count is $null, so the
    # result was silently dropped and the summary showed "n/a".
    $expoResults = @(Test-ExpoReadiness)
    if ($expoResults.Count -gt 0) {
        $results += $expoResults
        if (-not $expoResults[0].Ok) { $exitCode = 1 }
    }

    # --- Step 10 ------------------------------------------------------------- #
    Write-FinalSummary -Results $results -Ip $script:LanIp

    $script:Completed = $true
} catch {
    Write-Err ("unexpected error: {0}" -f $_.Exception.Message)
    Write-Info 're-run with .\stop.bat, then .\start.bat -Rebuild'
    $exitCode = 1
} finally {
    # Ctrl+C in this window must not leave orphans behind (local mode only:
    # in docker mode the containers are designed to survive).
    if (-not $script:Completed -and $script:EffectiveMode -eq 'local' -and $Reset -eq $false) {
        Write-Warn2 'interrupted - stopping the services this script started'
        foreach ($service in @('backend', 'citizen', 'police', 'expo')) {
            try { Stop-TrackedService -Name $service } catch { }
        }
    }
}
exit $exitCode
