#Requires -Version 5.1
<#
.SYNOPSIS
    Stops everything scripts/start.ps1 started.

.DESCRIPTION
    docker mode : docker compose down (containers and network removed, volumes kept).
    local mode  : kills the process trees recorded in .rakshak/pids.json and frees
                  TCP 8000 / 5173 / 5174 / 8081 if something is still bound.

    PostgreSQL and Redis are Windows services and are intentionally left running.

.PARAMETER Mode
    auto (default - reads .rakshak/pids.json), docker or local.

.PARAMETER Ports
    Extra ports to free, comma separated. Default: 8000,5173,5174,8081.

.EXAMPLE
    .\scripts\stop.ps1
    .\stop.bat
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'docker', 'local')]
    [string] $Mode = 'auto',

    [string] $Ports = '8000,5173,5174,8081',

    [switch] $RemoveVolumes
)

$ErrorActionPreference = 'Continue'

$ScriptDir = $PSScriptRoot
if ([string]::IsNullOrEmpty($ScriptDir)) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$RepoRoot = Split-Path -Parent $ScriptDir
$StateDir = Join-Path $RepoRoot '.rakshak'
$PidsPath = Join-Path $StateDir 'pids.json'

$ServiceNames = @('backend', 'citizen', 'police', 'expo')
$stopped = New-Object System.Collections.Generic.List[string]

function Write-Step { param([string]$Text) Write-Host ("  -> " + $Text) -ForegroundColor Cyan }
function Write-Ok { param([string]$Text) Write-Host ("  [ OK ] " + $Text) -ForegroundColor Green }
function Write-Info { param([string]$Text) Write-Host ("  [info] " + $Text) -ForegroundColor Gray }
function Write-Warn2 { param([string]$Text) Write-Host ("  [warn] " + $Text) -ForegroundColor Yellow }
function Write-Err { param([string]$Text) Write-Host ("  [FAIL] " + $Text) -ForegroundColor Red }

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

function Stop-ProcessTree {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return }
    try { & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null } catch { }
    Start-Sleep -Milliseconds 400
    try {
        $still = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($still) { Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
}

function Get-PortOwner {
    <#
        Decides whether the process holding a port belongs to this project.

        The port is on ours if it was launched from this repo, or if it is one of
        the runtimes start.ps1 uses (Expo/Metro, Vite, uvicorn). A process whose
        command line cannot be read is never assumed to be ours - guessing there
        would mean killing something the user cares about.
    #>
    param([int]$ProcessId)

    $cmd = ''
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if ($cim -and $cim.CommandLine) { $cmd = [string]$cim.CommandLine }
    } catch { }

    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    $name = if ($proc) { $proc.ProcessName } else { 'unknown process' }

    if (-not $cmd) {
        return [pscustomobject]@{ Ours = $false; Reason = "$name (pid $ProcessId), command line unavailable"; CommandLine = '' }
    }
    if ($cmd -like "*$RepoRoot*") {
        return [pscustomobject]@{ Ours = $true; Reason = 'running from this repo'; CommandLine = $cmd }
    }
    if ($cmd -match '(?i)expo(\\|/)bin(\\|/)cli|metro|@expo(\\|/)metro') {
        return [pscustomobject]@{ Ours = $true; Reason = "Expo/Metro ($name)"; CommandLine = $cmd }
    }
    if ($cmd -match '(?i)vite(\\|/)bin(\\|/)vite\.js') {
        return [pscustomobject]@{ Ours = $true; Reason = "Vite dev server ($name)"; CommandLine = $cmd }
    }
    if ($cmd -match '(?i)uvicorn') {
        return [pscustomobject]@{ Ours = $true; Reason = "uvicorn server ($name)"; CommandLine = $cmd }
    }
    return [pscustomobject]@{ Ours = $false; Reason = "$name (pid $ProcessId) is not a RAKSHAK process"; CommandLine = $cmd }
}

function Read-PidMap {
    $map = @{}
    if (-not (Test-Path -LiteralPath $PidsPath)) { return $map }
    try {
        $json = Get-Content -LiteralPath $PidsPath -Raw | ConvertFrom-Json
        if ($null -eq $json) { return $map }
        $holder = $json
        if ($json.PSObject.Properties.Name -contains 'services') { $holder = $json.services }
        if ($null -eq $holder) { return $map }
        foreach ($prop in $holder.PSObject.Properties) {
            $value = $prop.Value
            $pid_ = 0
            $label = $prop.Name
            if ($value -is [int] -or $value -is [long]) { $pid_ = [int]$value }
            else {
                if ($value.PSObject.Properties.Name -contains 'pid') { $pid_ = [int]$value.pid }
                if ($value.PSObject.Properties.Name -contains 'label') { $label = [string]$value.label }
            }
            if ($pid_ -gt 0) { $map[$prop.Name] = @{ Pid = $pid_; Label = $label } }
        }
    } catch { }
    return $map
}

function Stop-Docker {
    Write-Step 'docker compose down'
    Push-Location $RepoRoot
    try {
        if ($RemoveVolumes) {
            & docker compose down -v 2>&1 | Out-String | Write-Info
            Write-Ok 'containers, network and volumes removed'
        } else {
            & docker compose down 2>&1 | Out-String | Write-Info
            Write-Ok 'containers and network removed (volumes kept - use -RemoveVolumes to drop the database)'
        }
    } finally {
        Pop-Location
    }
    $script:stopped.Add('docker containers (postgres, redis, backend, citizen_web, police_dashboard)')
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host ('=' * 74) -ForegroundColor DarkGray
Write-Host '  RAKSHAK - stop' -ForegroundColor Cyan
Write-Host ('=' * 74) -ForegroundColor DarkGray

$dockerAvailable = $false
$dockerCli = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCli) {
    & $dockerCli.Source info *> $null
    if ($LASTEXITCODE -eq 0) { $dockerAvailable = $true }
}

$recorded = Read-PidMap
$recordedMode = ''
if (Test-Path -LiteralPath $PidsPath) {
    try {
        $j = Get-Content -LiteralPath $PidsPath -Raw | ConvertFrom-Json
        if ($j.PSObject.Properties.Name -contains 'mode') { $recordedMode = [string]$j.mode }
    } catch { }
}

if ($Mode -eq 'auto') {
    if ($recordedMode -eq 'docker') { $effective = 'docker' }
    elseif ($recordedMode -eq 'local') { $effective = 'local' }
    elseif ($dockerAvailable) { $effective = 'docker' }
    else { $effective = 'local' }
} else {
    $effective = $Mode
}

Write-Info ("mode: {0}" -f $effective)

if ($effective -eq 'docker' -and -not $dockerAvailable) {
    Write-Warn2 'Docker is not running, so there is nothing to stop for the container stack'
    $effective = 'local'
}

# --- docker: compose down ---------------------------------------------------- #
if ($effective -eq 'docker') {
    Stop-Docker
}

# --- local: tracked processes ------------------------------------------------ #
foreach ($name in $ServiceNames) {
    if (-not $recorded.ContainsKey($name)) { continue }
    $entry = $recorded[$name]
    $procId = [int]$entry.Pid
    $label = [string]$entry.Label

    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        Write-Info ("{0}: already gone (pid {1})" -f $label, $procId)
        $stopped.Add("$label (was not running)")
        continue
    }
    Write-Step ("stopping {0} (pid {1}, {2})" -f $label, $procId, $proc.ProcessName)
    Stop-ProcessTree -ProcessId $procId
    $stopped.Add("$label (pid $procId)")
}

# --- local: free the ports --------------------------------------------------- #
$portList = @()
foreach ($p in ($Ports -split ',')) {
    $trimmed = $p.Trim()
    if ($trimmed -match '^\d+$') { $portList += [int]$trimmed }
}

foreach ($port in $portList) {
    $pid_ = Get-ListeningPid -Port $port
    if ($pid_ -le 0) {
        Write-Ok ("TCP {0} is free" -f $port)
        continue
    }

    # Only kill what belongs to this project.
    #
    # This loop used to taskkill whatever held the port. After a laptop sleep or a
    # closed window the port is often held by a leftover Metro of ours, but it
    # can also be some unrelated program that happens to use 8081 - and silently
    # killing that is both destructive and mystifying. So the owning process is
    # identified first, and a stranger is reported by name instead of stopped.
    $verdict = Get-PortOwner -ProcessId $pid_
    if (-not $verdict.Ours) {
        Write-Warn2 ("TCP {0} is held by a program that is not part of RAKSHAK: {1}" -f $port, $verdict.Reason)
        if ($verdict.CommandLine) { Write-Host ('      ' + $verdict.CommandLine) -ForegroundColor DarkYellow }
        Write-Info  'left it running; close it yourself if the next start complains about the port'
        continue
    }

    $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
    $procName = if ($proc) { $proc.ProcessName } else { 'unknown' }
    Write-Step ("TCP {0} still held by {1} ({2}, pid {3}) - stopping it" -f $port, $procName, $verdict.Reason, $pid_)
    Stop-ProcessTree -ProcessId $pid_
    Start-Sleep -Milliseconds 500
    if ((Get-ListeningPid -Port $port) -gt 0) {
        Write-Warn2 ("TCP {0} is STILL held - close the app using it manually" -f $port)
    } else {
        Write-Ok ("TCP {0} is free" -f $port)
        $stopped.Add("process on TCP $port ($procName, pid $pid_)")
    }
}

# --- local: drop the pid file so the next start is clean --------------------- #
if (Test-Path -LiteralPath $PidsPath) {
    Remove-Item -LiteralPath $PidsPath -Force -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host '  Stopped:' -ForegroundColor Cyan
if ($stopped.Count -eq 0) {
    Write-Host '    (nothing was running)' -ForegroundColor Gray
} else {
    foreach ($item in $stopped) { Write-Host ("    - " + $item) -ForegroundColor Gray }
}

Write-Host ''
Write-Info 'PostgreSQL and Redis are Windows services and were left running (they are not part of the demo stack).'
Write-Host ''
Write-Host '  Start again with .\start.bat' -ForegroundColor DarkGray
Write-Host ''
exit 0