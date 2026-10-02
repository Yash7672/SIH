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

    [switch] $SkipFirewall,

    [switch] $NoWatch
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
$StatePath = Join-Path $StateDir 'state.json'
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
$script:NetAdapter = ''
$script:NetGateway = ''
$script:NetMetric = -1
$script:NetProfile = 'unknown'
$script:EffectiveMode = 'local'
$script:Fresh = $true     # $false => every tracked service was already running
$script:ExpoPending = $false
$script:ExpoRunning = $false
$script:PhoneState = 'not-checked'   # authorized | unauthorized | none | no-adb
$script:AdbDownloaded = $false
$script:WatchJob = $null
$script:Restarting = $false
# The address the PREVIOUS run recorded, captured before state.json is
# overwritten. Start-Expo needs it to tell "reusing Metro" from "restart it".
$script:PreviousLanIp = ''

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

function Get-ProcessCommandLine {
    <#
        Needed before killing anything. The owning PID is on its own not enough to
        tell "a leftover Metro from a crashed run" from "some other program that
        happens to use port 8081" - killing the latter would be destructive and
        confusing.
    #>
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return '' }
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if ($p -and $p.CommandLine) { return [string]$p.CommandLine }
    } catch { }
    return ''
}

function Test-ProjectProcess {
    <#
        True when the process looks like ours: it was launched from this repo, or
        it is one of the runtimes start.ps1 uses (expo/Metro, vite, uvicorn).

        Returns an object with Reason so the caller can print *why* it decided,
        which matters when the user is looking at "stopping node.exe".
    #>
    param([int]$ProcessId)

    $cmd = Get-ProcessCommandLine -ProcessId $ProcessId
    $proc = Get-ProcessInfo $ProcessId
    $name = if ($proc) { $proc.ProcessName } else { 'unknown' }

    if (-not $cmd) {
        # No command line available (permissions, or already gone). Never guess
        # that a process we cannot identify is ours.
        return [pscustomobject]@{ Ours = $false; Reason = "command line unavailable for $name (pid $ProcessId)" }
    }

    if ($cmd -like "*$RepoRoot*") {
        return [pscustomobject]@{ Ours = $true; Reason = "running from this repo ($name)" }
    }
    if ($cmd -match '(?i)expo(\\|/)bin(\\|/)cli|metro|@expo(\\|/)metro') {
        return [pscustomobject]@{ Ours = $true; Reason = "Expo/Metro process ($name)" }
    }
    if ($cmd -match '(?i)vite(\\|/)bin(\\|/)vite\.js') {
        return [pscustomobject]@{ Ours = $true; Reason = "Vite dev server ($name)" }
    }
    if ($cmd -match '(?i)uvicorn') {
        return [pscustomobject]@{ Ours = $true; Reason = "uvicorn server ($name)" }
    }

    return [pscustomobject]@{ Ours = $false; Reason = "$name (pid $ProcessId) is not a RAKSHAK process" }
}

function Clear-OrphanOnPort {
    <#
        Frees a port that a previous run left behind.

        Called before starting a service. Only processes that can be attributed to
        this project are killed; anything else is reported by name so the user can
        decide, instead of the script silently taking the port or silently dying.
    #>
    param(
        [int] $Port,
        [string] $ServiceName,
        [switch] $Silent
    )

    $ownerPid = Get-ListeningPid -Port $Port
    if ($ownerPid -le 0) { return $true }

    $verdict = Test-ProjectProcess -ProcessId $ownerPid
    if (-not $verdict.Ours) {
        if (-not $Silent) {
            Write-Warn2 ("TCP {0} is held by {1} - not a RAKSHAK process, leaving it alone" -f $Port, $verdict.Reason)
        }
        return $false
    }

    if (-not $Silent) {
        Write-Warn2 ("cleaning up stale {0} on TCP {1} (pid {2}, {3})" -f $ServiceName, $Port, $ownerPid, $verdict.Reason)
    }
    Stop-ProcessTree -ProcessId $ownerPid

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Get-ListeningPid -Port $Port) -gt 0) {
        Start-Sleep -Milliseconds 250
    }
    if ((Get-ListeningPid -Port $Port) -gt 0) {
        Write-Warn2 ("TCP {0} is still held after stopping pid {1}" -f $Port, $ownerPid)
        return $false
    }
    if (-not $Silent) {
        Write-Ok ("TCP {0} is free" -f $Port)
    }
    return $true
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
function Get-NetworkProfileName {
    <#
        Windows classifies a NEW network as Public, which blocks the inbound
        connections the phone needs. Surfaced so the user can see it.
    #>
    try {
        $profile = Get-NetConnectionProfile -ErrorAction Stop |
            Where-Object { $_.IPv4Connectivity -ne 'Disconnected' } |
            Select-Object -First 1
        if ($profile) { return [string]$profile.NetworkCategory }
    } catch { }
    return 'unknown'
}

function Get-LanIpv4 {
    <#
        Picks the address a phone would actually reach us on.

        Ranking is by ROUTE METRIC of the default route (0.0.0.0/0) that the
        adapter holds, lowest wins - that is what Windows itself uses to decide
        where outbound traffic goes, so it stays correct with a VPN, WSL,
        VirtualBox or a phone hotspot layered on top. Virtual adapters, APIPA
        (169.254.x.x, i.e. "no DHCP lease yet") and loopback are excluded, and
        an adapter that is not Up is skipped.
    #>
    param([string] $Override, [int] $WaitSeconds = 0)

    if ($Override) {
        if ($Override -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
            Die "-HostIp '$Override' is not a valid IPv4 address (example: -HostIp 192.168.1.20)"
        }
        Write-Info ("using -HostIp override: {0}" -f $Override)
        $script:NetAdapter = 'pinned with -HostIp'
        $script:NetGateway = ''
        $script:NetMetric = -1
        return $Override
    }

    $excluded = 'vEthernet|VirtualBox|VMware|WSL|Loopback|Hyper-V|Docker|WSL2|vbox|vmnet|TAP|Tailscale|ZeroTier|Bluetooth|Loopback Pseudo|Npcap'
    $deadline = (Get-Date).AddSeconds([Math]::Max($WaitSeconds, 0))

    while ($true) {
        $candidates = New-Object System.Collections.Generic.List[object]

        # Lowest route metric wins, so a VPN or virtual switch with a small
        # metric cannot hijack the choice.
        $metricByAlias = @{}
        try {
            Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | ForEach-Object {
                $alias = [string]$_.InterfaceAlias
                $metric = [int]$_.RouteMetric
                if (-not $metricByAlias.ContainsKey($alias) -or $metric -lt $metricByAlias[$alias]) {
                    $metricByAlias[$alias] = $metric
                }
            }
        } catch { }

        try {
            $configs = Get-NetIPConfiguration -ErrorAction Stop
            foreach ($cfg in $configs) {
                $alias = [string]$cfg.InterfaceAlias
                if ($alias -match $excluded) { continue }

                $up = $true
                try { if ($cfg.NetAdapter -and $cfg.NetAdapter.Status -ne 'Up') { $up = $false } } catch { }
                if (-not $up) { continue }

                if (-not $cfg.IPv4Address) { continue }
                $gateway = ''
                if ($cfg.IPv4DefaultGateway) {
                    foreach ($gw in $cfg.IPv4DefaultGateway) {
                        if ([string]$gw.NextHop) { $gateway = [string]$gw.NextHop; break }
                    }
                }

                $metric = if ($metricByAlias.ContainsKey($alias)) { $metricByAlias[$alias] } else { 9999 }

                foreach ($v4 in $cfg.IPv4Address) {
                    $ip = [string]$v4.IPAddress
                    if (-not $ip) { continue }
                    if ($ip.StartsWith('127.')) { continue }      # loopback
                    if ($ip.StartsWith('169.254.')) { continue }  # APIPA: no DHCP lease

                    # Lower total wins. Route metric is the primary signal
                    # (Windows' own rule); a real gateway breaks ties.
                    $score = (10000 - [Math]::Min($metric, 9999))
                    if ($gateway) { $score += 5000 }
                    if ($alias -match 'Wi-Fi|Wireless|WLAN|Ethernet') { $score += 200 }
                    $candidates.Add([pscustomobject]@{
                        Ip = $ip; Alias = $alias; Gateway = $gateway; Score = $score; Metric = $metric
                    })
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
                        $candidates.Add([pscustomobject]@{
                            Ip = $ip; Alias = $current; Gateway = ''; Score = 1; Metric = -1
                        })
                    }
                }
            } catch { }
        }

        if ($candidates.Count -gt 0) {
            $best = $candidates | Sort-Object -Property Score -Descending | Select-Object -First 1
            $script:NetAdapter = [string]$best.Alias
            $script:NetGateway = [string]$best.Gateway
            $script:NetMetric = [int]$best.Metric

            if ($candidates.Count -gt 1) {
                $others = ($candidates | Where-Object { $_.Ip -ne $best.Ip } |
                    ForEach-Object { "$($_.Ip) ($($_.Alias), metric $($_.Metric))" }) -join ', '
                Write-Warn2 ("more than one usable adapter - using {0}. Ignored: {1}" -f $best.Alias, $others)
                Write-Info 'override the choice with -HostIp if the phone is on a different network'
            }
            return [string]$best.Ip
        }

        if ((Get-Date) -ge $deadline) { break }
        # A machine that just woke from sleep, or just joined Wi-Fi, often needs
        # a few seconds before DHCP hands out an address.
        Start-Sleep -Seconds 2
    }

    Write-Warn2 'Could not detect a LAN IPv4 address automatically.'
    Write-Warn2 'Falling back to 127.0.0.1 - a phone will NOT be able to reach the demo.'
    Write-Warn2 'Re-run with an explicit address if needed:  .\start.bat -HostIp 192.168.1.20'
    $script:NetAdapter = 'none'
    $script:NetGateway = ''
    $script:NetMetric = -1
    return '127.0.0.1'
}

# --------------------------------------------------------------------------- #
# Step 3 + 4 - env files
# --------------------------------------------------------------------------- #
function Initialize-EnvFiles {
    param([string] $Ip, [switch] $PinIp)

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
CORS_ALLOW_PRIVATE_NETWORK=true

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

    # CORS deliberately does NOT list the current LAN IP.
    #
    # It used to, and that was the root of the "it works until I change network"
    # bug: the list only takes effect when uvicorn restarts, so a new IP meant
    # either a stale allow-list (browser CORS errors, dead live feed) or a forced
    # backend restart on every network change. main.py now also accepts any
    # loopback/private-LAN origin via CORS_ALLOW_PRIVATE_NETWORK, so the explicit
    # list stays IP-independent and .env stops churning.
    $cors = 'http://localhost:5173,http://localhost:5174'
    Set-DotEnvValue -Path $EnvPath -Key 'CORS_ORIGINS' -Value $cors
    Set-DotEnvValue -Path $EnvPath -Key 'CORS_ALLOW_PRIVATE_NETWORK' -Value 'true'
    Set-DotEnvValue -Path $EnvPath -Key 'API_BASE_URL' -Value $apiBase

    # VITE_API_URL / EXPO_PUBLIC_API_URL are left EMPTY unless the IP was pinned
    # explicitly with -HostIp.
    #
    # Vite inlines VITE_* into the JS bundle at build time and Metro inlines
    # EXPO_PUBLIC_* at bundle time, so writing the IP here froze one address into
    # the bundle and the client kept dialling it after the router moved the PC.
    # Every client now derives its host at runtime instead (see the resolve*
    # helpers in the web apps and mobile_app/src/services/api.js), so these are
    # only written when a specific address was demanded.
    if ($PinIp) {
        Set-DotEnvValue -Path $EnvPath -Key 'VITE_API_URL' -Value $apiBase
        Set-DotEnvValue -Path $EnvPath -Key 'VITE_WS_URL' -Value $wsBase
        Set-DotEnvValue -Path $EnvPath -Key 'EXPO_PUBLIC_API_URL' -Value $apiBase
        Write-Warn2 ("pinned VITE_API_URL / EXPO_PUBLIC_API_URL to {0} (-HostIp). Clear these to go back to auto-detection." -f $apiBase)
    } else {
        Set-DotEnvValue -Path $EnvPath -Key 'VITE_API_URL' -Value ''
        Set-DotEnvValue -Path $EnvPath -Key 'VITE_WS_URL' -Value ''
        Set-DotEnvValue -Path $EnvPath -Key 'EXPO_PUBLIC_API_URL' -Value ''
        Write-Ok ('.env updated: CORS_ORIGINS (private LAN allowed), API_BASE_URL -> {0}; client URLs left empty for runtime resolution' -f $apiBase)
    }

    # Frontend env files. Same rule: empty unless the IP was pinned, so a network
    # change never requires rebuilding either web app.
    $webEnv = if ($PinIp) {
@"
# Generated by scripts/start.ps1 - host PINNED with -HostIp.
VITE_API_URL=$apiBase
VITE_WS_URL=$wsBase
"@
    } else {
@"
# Generated by scripts/start.ps1. Left empty on purpose: each app derives the
# backend host from window.location at runtime, so it keeps working when the
# router hands this PC a new address. Set VITE_API_URL here to pin one.
VITE_API_URL=
VITE_WS_URL=
"@
    }
    Write-TextFile (Join-Path $CitizenDir '.env.local') $webEnv
    Write-TextFile (Join-Path $PoliceDir '.env.local') $webEnv

    $mobileEnv = if ($PinIp) {
@"
# Generated by scripts/start.ps1. EXPO_PUBLIC_API_URL PINNED with -HostIp.
EXPO_PUBLIC_API_URL=$apiBase
"@
    } else {
@"
# Generated by scripts/start.ps1. Left empty on purpose: the app reads the host
# Metro is serving from (Constants.expoConfig.hostUri), so it follows the PC to
# a new network automatically after you rescan the QR in Expo Go.
EXPO_PUBLIC_API_URL=
"@
    }
    Write-TextFile (Join-Path $MobileDir '.env') $mobileEnv
    Write-Ok 'wrote citizen_web/.env.local, police_dashboard/.env.local, mobile_app/.env'

    Import-DotEnv -Path $EnvPath
}

# --------------------------------------------------------------------------- #
# Step 5 - firewall
# --------------------------------------------------------------------------- #
function Initialize-Firewall {
    <#
        Opens 8000 / 5173 / 5174 / 8081 for inbound traffic.

        Windows classifies a network it has not seen before as Public, and Public
        blocks inbound by default - so joining a new Wi-Fi silently breaks the
        phone with no error anywhere. The rules are therefore created with
        -Profile Any rather than Private only, and any older RAKSHAK-* rule is
        replaced, because a rule left over from a previous run with a narrower
        profile would otherwise keep blocking the port.
    #>
    param([string] $Ip)

    $definitions = @(
        @{ Port = 8000; Name = 'RAKSHAK-API-8000';  What = 'FastAPI backend' },
        @{ Port = 5173; Name = 'RAKSHAK-CITIZEN-5173'; What = 'citizen web' },
        @{ Port = 5174; Name = 'RAKSHAK-POLICE-5174';  What = 'police dashboard' },
        @{ Port = 8081; Name = 'RAKSHAK-EXPO-8081';   What = 'Expo Metro (QR code)' }
    )

    if ($SkipFirewall) {
        Write-Info 'firewall step skipped (-SkipFirewall)'
        return
    }

    # Report the active profile: on a fresh network this is what decides whether
    # the rules below matter at all.
    $script:NetProfile = Get-NetworkProfileName
    $profileText = if ($script:NetProfile -eq 'unknown') { 'unknown' } else { [string]$script:NetProfile }
    if ($profileText -eq 'Public') {
        # Worth saying out loud: a network Windows has not seen before is always
        # Public, and Public blocks inbound by default, so a newly joined Wi-Fi or
        # hotspot is the classic reason the phone suddenly cannot reach the PC.
        Write-Info ('active network profile: Public (inbound blocked unless an allow rule exists)')
    } else {
        Write-Info ("active network profile: {0}" -f $profileText)
    }

    # The admin one-liner is built first so it can be printed in either branch.
    $oneLiner = 'Get-NetFirewallRule -DisplayName RAKSHAK-* -ErrorAction SilentlyContinue | Remove-NetFirewallRule; ' +
        (($definitions | ForEach-Object {
            "New-NetFirewallRule -DisplayName '$($_.Name)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $($_.Port) -Profile Any"
        }) -join '; ')

    if (-not (Test-Admin)) {
        Write-Info 'not running as Administrator - the firewall step cannot change anything'
        # Reading rules works without admin, so we can still tell the user whether
        # they need to do anything at all instead of always shouting.
        $script:HasFirewallRules = @(Get-NetFirewallRule -DisplayName 'RAKSHAK-*' -ErrorAction SilentlyContinue)
        if ($script:HasFirewallRules.Count -gt 0) {
            Write-Ok ('RAKSHAK firewall rules already present ({0}) - nothing to do' -f (($script:HasFirewallRules | ForEach-Object { $_.DisplayName }) -join ', '))
            return
        }

        # No RAKSHAK rules - but that is not automatically a problem. When Windows
        # asks "Allow access" the first time a server binds a port it creates
        # *program*-scoped rules (node.exe, python.exe) instead, and those already
        # cover all four ports. Check for those before warning: warning about a
        # blocked phone when the phone works fine trains the user to ignore the
        # message that matters.
        $programRules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue |
            Where-Object { $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' -and $_.Direction -eq 'Inbound' -and
                           $_.DisplayName -match '^(node|python)' })
        if ($programRules.Count -gt 0) {
            Write-Ok ('inbound is already allowed for {0} (program-scoped rules Windows created earlier)' -f
                (($programRules | ForEach-Object { $_.DisplayName } | Sort-Object -Unique) -join ', '))
            Write-Info 'these cover 8000 / 5173 / 5174 / 8081 on any network, including a new Wi-Fi'
            return
        }

        Write-Warn2 'no inbound allow rule exists, and the profile blocks it - the phone cannot reach this PC'
        Write-Warn2 'open PowerShell AS ADMINISTRATOR and paste this single line:'
        Write-Host ("      " + $oneLiner) -ForegroundColor DarkYellow
        return
    }

    foreach ($def in $definitions) {
        try {
            $existing = @(Get-NetFirewallRule -DisplayName $def.Name -ErrorAction SilentlyContinue)
            $needsReplace = $false
            foreach ($rule in $existing) {
                # A rule scoped to a profile that is not Any would keep blocking
                # the port on a newly-joined Public network.
                $profiles = [string]$rule.Profile
                if ($profiles -ne 'Any') { $needsReplace = $true }
            }
            if ($existing.Count -gt 0 -and -not $needsReplace) {
                Write-Info ("firewall rule '{0}' already exists (Profile Any)" -f $def.Name)
                continue
            }
            if ($needsReplace) {
                Write-Info ("replacing firewall rule '{0}' - it was limited to a narrower profile" -f $def.Name)
                $existing | Remove-NetFirewallRule -ErrorAction SilentlyContinue
            }
            New-NetFirewallRule -DisplayName $def.Name -Direction Inbound -Action Allow -Protocol TCP `
                -LocalPort $def.Port -Profile Any -Description "RAKSHAK demo: $($def.What)" | Out-Null
            Write-Ok ("firewall rule '{0}' opened on TCP {1} (profile Any)" -f $def.Name, $def.Port)
        } catch {
            Write-Warn2 ("could not add firewall rule '{0}': {1}" -f $def.Name, $_.Exception.Message)
        }
    }
}

function Write-ExpoQr {
    <#
        Prints a scannable QR for the Expo URL in the main window.

        Expo already prints one in its own window; this is the copy you can see
        without hunting for that window, and it is the thing that actually
        changes after a network switch, so it belongs next to the summary.

        Rendered by scripts/qr.js using the `toqr` encoder already present in
        mobile_app/node_modules - no new dependency. If node or the encoder is
        unavailable the URL is still printed, so this can never block a start.
    #>
    param([string] $Url)

    $qrScript = Join-Path $ScriptDir 'qr.js'
    if (-not (Test-Path -LiteralPath $qrScript)) {
        Write-Info "QR script missing - type this into Expo Go manually: $Url"
        return
    }

    $nodeExe = Test-Command -Name 'node'
    if (-not $nodeExe) {
        Write-Info "node not on PATH - type this into Expo Go manually: $Url"
        return
    }

    try {
        $lines = @(& $nodeExe $qrScript $Url 2>$null)
        $code = $LASTEXITCODE
    } catch {
        $lines = @()
        $code = 1
    }

    if ($code -ne 0 -or $lines.Count -eq 0) {
        Write-Info "could not render the QR code - type this into Expo Go manually: $Url"
        return
    }

    foreach ($line in $lines) {
        # Two leading spaces keeps the code aligned under the summary block.
        Write-Host ('  ' + $line) -ForegroundColor White
    }
}

function Write-NetworkState {
    <#
        Persists the detected network so the NEXT run can tell whether it moved.

        Without this, a second start.bat had no way to know the address had
        changed and reused a Metro that was still advertising the old one.
    #>
    param([string] $Ip)

    $state = Get-StateMap -Path $StatePath -Property 'state'
    $state['ip'] = $Ip
    $state['adapter'] = $script:NetAdapter
    $state['gateway'] = $script:NetGateway
    $state['profile'] = $script:NetProfile
    $state['updatedAt'] = (Get-Date).ToString('o')
    Write-JsonFile $StatePath ([pscustomobject]@{ state = $state })
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

    # Was it started by a previous run of this script? (pids.json survives a
    # crash, a closed window and a laptop sleep.)
    $map = Get-PidMap
    $tracked = $false
    if ($map.ContainsKey($ServiceName)) {
        $entry = $map[$ServiceName]
        $trackedPid = 0
        if ($entry -is [int] -or $entry -is [long]) { $trackedPid = [int]$entry }
        else { $trackedPid = [int]$entry.pid }
        if ($trackedPid -eq $ownerPid) { $tracked = $true }
    }

    $proc = Get-ProcessInfo $ownerPid
    $procName = if ($proc) { $proc.ProcessName } else { 'unknown process' }
    Write-Warn2 ("TCP {0} is already in use by {1} (pid {2})" -f $Port, $procName, $ownerPid)

    if ($tracked) {
        Write-Info "that is the $ServiceName this script started earlier - reusing it"
        return $true
    }

    # Not tracked. Only take the port if the process can be attributed to this
    # project - otherwise a stale Metro from a crashed run would block the start
    # forever, and an unrelated program would be killed by surprise.
    $verdict = Test-ProjectProcess -ProcessId $ownerPid
    if ($verdict.Ours) {
        Write-Info ("untracked but recognisable as ours: {0}" -f $verdict.Reason)
        Stop-ProcessTree -ProcessId $ownerPid
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline -and (Get-ListeningPid -Port $Port) -gt 0) {
            Start-Sleep -Milliseconds 250
        }
        if ((Get-ListeningPid -Port $Port) -gt 0) {
            Die ("TCP {0} is still held after stopping pid {1} ({2})." -f $Port, $ownerPid, $verdict.Reason)
        }
        Write-Ok ("freed TCP {0}" -f $Port)
        return $true
    }

    # Someone else's program. Name it precisely and refuse, rather than guessing.
    $cmd = Get-ProcessCommandLine -ProcessId $ownerPid
    Write-Warn2 ("TCP {0} is held by a program that is not part of RAKSHAK: {1}" -f $Port, $verdict.Reason)
    if ($cmd) {
        Write-Host ('      ' + $cmd) -ForegroundColor DarkYellow
    }

    if ($AllowReuse) {
        Die ("TCP {0} is held by {1}. Stop it yourself, then re-run start.bat." -f $Port, $verdict.Reason)
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

function Stop-ExpoIfRunning {
    <#
        Metro is the one service that is useless after an IP change: it advertises
        the address it started with, so the phone cannot attach to a stale one.
        Reused processes are killed here rather than left running, which is what
        produced "No apps connected" and a QR code for an IP that no longer
        existed.
    #>
    $owner = Get-ListeningPid -Port $Ports.expo
    if ($owner -le 0) { return }
    Write-Warn2 ("stopping Expo Metro (pid {0}) - it advertises the old network address" -f $owner)
    Stop-ProcessTree -ProcessId $owner
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and (Get-ListeningPid -Port $Ports.expo) -gt 0) {
        Start-Sleep -Milliseconds 250
    }
    $script:ExpoRunning = $false
}

function Start-Expo {
    if ($NoMobile) {
        Write-Info 'mobile app skipped (-NoMobile)'
        return
    }

    # The IP this run started on, compared against the address the PREVIOUS run
    # recorded. That value is captured in step 2, before Write-NetworkState
    # overwrites state.json - re-reading the file here would always see the
    # current IP and the restart would never trigger.
    $prevIp = [string]$script:PreviousLanIp
    $ipChanged = ($prevIp -ne '' -and $prevIp -ne $script:LanIp)

    if ((Get-ListeningPid -Port $Ports.expo) -gt 0) {
        if ($ipChanged) {
            Write-Warn2 ("LAN IP changed {0} -> {1}: restarting Metro so it advertises the new address" -f $prevIp, $script:LanIp)
            Stop-ExpoIfRunning
        } else {
            Write-Ok 'Expo Metro already running (reusing it)'
            $script:Fresh = $false
            $script:ExpoRunning = $true
            # The reuse path used to return before the phone was ever looked at,
            # so "plug the phone in, then re-run start.bat" could never work:
            # Metro was reused, --android was skipped and the summary said
            # SKIPPED. Detect the phone here too.
            $script:PhoneState = Get-PhoneConnection
            return
        }
    }

    Initialize-NodeDeps -Dir $MobileDir -Key 'mobile-lock' -AllowInstall | Out-Null
    Sync-ExpoSdk
    Assert-PortFree -Port $Ports.expo -ServiceName 'expo' -AllowReuse | Out-Null

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
    if ($Rebuild -or $configChanged -or $ipChanged) {
        $cliArgs += '--clear'
        Write-Info 'mobile_app: config or network changed since last run - starting Metro with --clear'
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

    # REACT_NATIVE_PACKAGER_HOSTNAME pins the address Metro advertises in the QR
    # and in exp:// URLs. Left unset, Metro picks an adapter itself and on a
    # laptop with a VPN, WSL or VirtualBox adapter that is regularly the wrong
    # one - the QR then points somewhere the phone cannot reach. set/ restores
    # afterwards so this script's own environment is untouched.
    $inner = '"' + $launcher.FilePath + '" ' + (($launcher.Arguments | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' ')
    $inner = 'cd /d "' + $MobileDir + '" && set "REACT_NATIVE_PACKAGER_HOSTNAME=' + $script:LanIp + '" && ' + $inner

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
    Write-Ok ("Expo Metro starting on {0} - QR code in the new window" -f $script:LanIp)
    Write-Host ('  Expo URL   : exp://{0}:{1}' -f $script:LanIp, $Ports.expo) -ForegroundColor Cyan
    Write-Info '  (in the Expo window: r reloads, a opens Android)'
    Write-Info '  "No apps connected" means the phone still points at the old address - rescan the QR.'
    $script:ExpoPending = $true
}

function Watch-Network {
    <#
        Polls the LAN address while the demo runs and restarts ONLY Metro when it
        moves.

        Metro bakes the address it advertises into its QR code and its exp:// URL,
        so after a network change it is the one process that is genuinely wrong.
        The backend and both Vite servers resolve the host at runtime and keep
        working untouched, so restarting anything else would be pure disruption.

        The address is re-read from Windows on each poll rather than re-detected
        from scratch, so a second adapter appearing cannot change the answer.
    #>
    param([string] $Ip, [int] $IntervalSeconds = 10)

    Write-Host ''
    Write-Host ("  Watching the network every {0}s - Ctrl+C in this window stops everything." -f $IntervalSeconds) -ForegroundColor DarkGray

    while ($true) {
        Start-Sleep -Seconds $IntervalSeconds
        if ($script:Completed) { return }

        try {
            $current = Get-LanIpv4
        } catch {
            continue
        }

        if ($current -eq $Ip) { continue }

        # Never re-enter while a restart is already in flight, or a flapping
        # adapter would start Metro repeatedly.
        if ($script:Restarting) { continue }
        $script:Restarting = $true
        try {
            Write-Host ''
            Write-Banner ("Network changed: {0} -> {1}" -f $Ip, $current) Yellow
            Write-Info 'only Metro is restarted: it is the one process that advertises an address'
            Write-Info 'the web apps and the backend follow the new IP on their own'

            $Ip = $current
            $script:LanIp = $current
            $script:NetProfile = Get-NetworkProfileName
            Write-NetworkState -Ip $current

            # Clear the recorded bundle hash so Start-Expo adds --clear: the QR and
            # the advertised URL are part of what a stale Metro would serve.
            try {
                $hashes = Get-StateMap -Path $HashesPath -Property 'hashes'
                $hashes['expo-config'] = 'network-changed'
                Write-JsonFile $HashesPath ([pscustomobject]@{ hashes = $hashes })
            } catch { }

            $script:ExpoRunning = $false
            Start-Expo
            Resolve-ExpoPid

            $newUrl = "exp://{0}:8081" -f $current
            Write-Host ''
            Write-Host ('  New Expo URL : {0}' -f $newUrl) -ForegroundColor Cyan
            Write-Host '  Rescan the QR in Expo Go - the old one points at an address that no longer exists.' -ForegroundColor Yellow
            Write-Host ('  Phone test   : http://{0}:8000/healthz' -f $current) -ForegroundColor DarkGray
            Write-ExpoQr -Url $newUrl
        } catch {
            Write-Warn2 ("could not restart Metro after the network change: {0}" -f $_.Exception.Message)
        } finally {
            $script:Restarting = $false
        }
    }
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

    # Which adapter the address came from. With a VPN, WSL, VirtualBox or a
    # hotspot layered on there is more than one plausible candidate, and the
    # usual cause of "the phone cannot reach it" is simply having picked the wrong
    # one - so the choice is shown rather than hidden.
    Write-Host ''
    Write-Host '  Network' -ForegroundColor Yellow
    $metricText = if ($script:NetMetric -ge 0) { "route metric $($script:NetMetric)" } else { 'route metric n/a' }
    $gatewayText = if ($script:NetGateway) { $script:NetGateway } else { 'none' }
    Write-Host '    Adapter   : ' -NoNewline -ForegroundColor DarkGray
    Write-Host ("{0}  ({1}, gateway {2})" -f $script:NetAdapter, $metricText, $gatewayText) -ForegroundColor Cyan
    Write-Host '    Profile   : ' -NoNewline -ForegroundColor DarkGray
    if ($script:NetProfile -eq 'Public') {
        Write-Host 'Public  (inbound needs an allow rule - checked below)' -ForegroundColor Cyan
    } else {
        Write-Host $script:NetProfile -ForegroundColor Cyan
    }
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
    $expoUrl = "exp://${Ip}:8081"
    Write-Host $expoUrl -ForegroundColor Cyan

    # Where the app will look for the backend. It derives this from the Metro host
    # at runtime, so it follows the PC onto a new network once the QR is rescanned
    # - no .env holds an address any more.
    Write-Host '    API target : ' -NoNewline -ForegroundColor DarkGray
    Write-Host ('http://{0}:8000  (derived from the Metro host at runtime)' -f $Ip) -ForegroundColor Cyan

    if (-not $NoMobile) {
        # The QR code, printed here as well as in the Expo window.
        Write-Host ''
        Write-Host ('  Phone: open Expo Go and scan the QR (or Enter URL manually: exp://{0}:8081).' -f $Ip) -ForegroundColor Green
        Write-Host '  After switching network you must rescan once.' -ForegroundColor Green
        Write-Host ''
        Write-ExpoQr -Url $expoUrl
    }

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

    # The single most useful diagnostic when the phone cannot connect: if the
    # backend is reachable from the phone browser, the problem is the QR/Metro
    # side, not the network.
    Write-Host ('  Phone test : open http://{0}:8000/healthz in the phone browser.' -f $Ip) -ForegroundColor DarkGray
    Write-Host '  If that does not load: same Wi-Fi? hotspot/guest or router "AP isolation" blocks device-to-device traffic.' -ForegroundColor DarkGray

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
    $script:LanIp = Get-LanIpv4 -Override $HostIp -WaitSeconds 20
    Write-Step ("LAN IP    : {0}" -f $script:LanIp)

    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

    # Remember the address, and say so when it moved since the previous run. This
    # is what makes a second run after a network switch behave differently from
    # the first: Metro is restarted because the URL it advertises is now wrong.
    $previousIp = ''
    if (Test-Path -LiteralPath $StatePath) {
        $prevState = Get-StateMap -Path $StatePath -Property 'state'
        if ($prevState.ContainsKey('ip')) { $previousIp = [string]$prevState['ip'] }
    }
    # Kept for Start-Expo, which runs later and after state.json has been
    # overwritten with the current address.
    $script:PreviousLanIp = $previousIp

    $script:NetProfile = Get-NetworkProfileName
    Write-NetworkState -Ip $script:LanIp
    if ($previousIp -and $previousIp -ne $script:LanIp) {
        Write-Warn2 ("network changed since the last run: {0} -> {1}" -f $previousIp, $script:LanIp)
        Write-Info 'Metro restarts to advertise the new address; the web apps and backend keep running'
    }

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
    # -PinIp only when the address was given explicitly. Otherwise the generated
    # client env files are left empty so each app resolves its host at runtime and
    # keeps working after the next network change.
    Initialize-EnvFiles -Ip $script:LanIp -PinIp:([bool]$HostIp)

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

    # --- Step 11: watch for the network changing under us --------------------- #
    # Only Metro is restarted when the address moves, because Metro is the one
    # process that publishes an address. Ctrl+C stops the watcher and, in the
    # finally block below, the child processes.
    if ($NoWatch) {
        Write-Info 'network watching skipped (-NoWatch) - re-run start.bat after changing network to pick up the new IP'
    } elseif ($NoMobile) {
        Write-Info 'network watching skipped (-NoMobile)'
    } else {
        try {
            Watch-Network -Ip $script:LanIp -IntervalSeconds 10
        } catch {
            Write-Warn2 ("network watcher stopped: {0}" -f $_.Exception.Message)
        }
    }

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
