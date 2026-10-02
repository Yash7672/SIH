# RAKSHAK end-to-end demo verification script (Windows PowerShell 5.1)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\e2e_verify.ps1 [-BaseUrl http://127.0.0.1:8000]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000"
)
$ErrorActionPreference = "Stop"
$base = "$BaseUrl/api/v1"

function Post-Json($path, $token, $body) {
    $headers = @{}
    if ($token) { $headers["Authorization"] = "Bearer $token" }
    Invoke-RestMethod -Method Post -Uri "$base$path" -Headers $headers -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 6)
}
function Get-Api($path, $token) {
    $headers = @{}
    if ($token) { $headers["Authorization"] = "Bearer $token" }
    Invoke-RestMethod -Method Get -Uri "$base$path" -Headers $headers
}
function Patch-Api($path, $token, $body) {
    $headers = @{ Authorization = "Bearer $token" }
    Invoke-RestMethod -Method Patch -Uri "$base$path" -Headers $headers -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 6)
}
function Expect-Error($label, $code) {
    if ($code -eq 0) { throw "$label should have failed but returned HTTP 2xx" } else {
        Write-Output ("PASS  RBAC {0} -> HTTP {1}" -f $label, $code)
    }
}

$section = 0
function Section($name) { $script:section++; Write-Output ("`n=== [{0}] {1} ===" -f $script:section, $name) }

Section "Login (citizen/cop/volunteer)"
$cit = Post-Json "/auth/login" $null @{ email = "citizen@example.com"; password = "Citizen@123" }
$cop = Post-Json "/auth/login" $null @{ email = "cop@example.com"; password = "Police@123" }
$vol = Post-Json "/auth/login" $null @{ email = "volunteer@example.com"; password = "Volunteer@123" }
$citT = $cit.access_token; $copT = $cop.access_token; $volT = $vol.access_token
Write-Output ("PASS  logins: {0}/{1}/{2}" -f $cit.user.role, $cop.user.role, $vol.user.role)

Section "Citizen files complaint (multipart)"
$c = & curl.exe -s -X POST "$base/complaints" -H "Authorization: Bearer $citT" `
    -F "plate=TS-09 AB 1234" -F "complaint_type=Stolen vehicle" `
    -F "description=Vehicle missing since Sunday near Ameerpet."
$c = $c | ConvertFrom-Json
Write-Output ("PASS  complaint created: plate={0} status={1}" -f $c.plate, $c.status)
$complaintId = $c.id

Section "Citizen sees own complaint via /complaints/mine"
$mine = Get-Api "/complaints/mine" $citT
Write-Output ("PASS  citizen has {0} complaint(s)" -f $mine.Count)

Section "Cop verifies complaint -> auto-hotlist"
$v = Post-Json "/complaints/$complaintId/verify" $copT @{ fir_reference = "FIR/2026/0099" }
Write-Output ("PASS  verified: complaint={0} hotlisted={1}" -f $v.complaint.status, $v.hotlist_id)
$hotlistId = $v.hotlist_id

Section "Hotlist reflects new entry"
$hot = Get-Api "/hotlist" $copT
$entry = $hot | Where-Object { $_.id -eq $hotlistId }
if (-not $entry) { throw "Hotlist entry not found" }
Write-Output ("PASS  hotlist entry: plate={0} status={1}" -f $entry.plate, $entry.status)

Section "Volunteer registers device"
$dev = Post-Json "/devices/register" $volT @{ device_type = "mobile"; device_name = "Nikhil Pixel 8 ANPR" }
Write-Output ("PASS  device registered: {0} revoked={1}" -f $dev.id, $dev.revoked)
$deviceId = $dev.id

Section "Volunteer submits detections (route + alert)"
# One device per detection: the backend cooldowns the same plate+device for 60s.
$dev2 = (Post-Json "/devices/register" $volT @{ device_type = "mobile"; device_name = "Cam-2 ANPR" }).id
$dev3 = (Post-Json "/devices/register" $volT @{ device_type = "mobile"; device_name = "Cam-3 ANPR" }).id
$pts = @(
    @{ plate = "TS09AB1234"; latitude = 17.4567; longitude = 78.3456; timestamp = "2026-09-22T09:00:00Z"; confidence = 0.96; device_id = $deviceId },
    @{ plate = "TS09AB1234"; latitude = 17.4610; longitude = 78.3520; timestamp = "2026-09-22T09:03:00Z"; confidence = 0.93; device_id = $dev2 },
    @{ plate = "TS09AB1234"; latitude = 17.4695; longitude = 78.3601; timestamp = "2026-09-22T09:06:00Z"; confidence = 0.98; device_id = $dev3 }
)
foreach ($p in $pts) {
    $s = Post-Json "/sightings" $volT $p
    Write-Output ("PASS  sighting recorded: {0} @ {1},{2}" -f $s.id.Substring(0, 8), $p.latitude, $p.longitude)
}

Section "Alerts surfaced to police"
$alerts = Get-Api "/alerts" $copT
Write-Output ("PASS  alert count for police: {0}" -f $alerts.Count)

Section "Analytics overview"
$ov = Get-Api "/analytics/overview" $copT
Write-Output ("PASS  overview: detections_today={0} active_hotlist={1}" -f $ov.detections_today, $ov.active_hotlist)

Section "Vehicle route"
$route = Get-Api "/vehicles/TS09AB1234/timeline" $copT
Write-Output ("PASS  route points: {0}" -f $route.Count)

Section "RBAC: citizen is denied"
try { Get-Api "/hotlist" $citT | Out-Null; Expect-Error "citizen->/hotlist" 0 } catch { Expect-Error "citizen->/hotlist" $_.Exception.Response.StatusCode.value__ }
try { Get-Api "/complaints" $citT | Out-Null; Expect-Error "citizen->/complaints" 0 } catch { Expect-Error "citizen->/complaints" $_.Exception.Response.StatusCode.value__ }
try { Post-Json "/sightings" (Post-Json "/auth/login" $null @{ email = "citizen@example.com"; password = "Citizen@123" }).access_token @{ plate = "TS09AB1234"; latitude = 17.4; longitude = 78.3; timestamp = "2026-09-22T10:00:00Z"; device_id = $deviceId } | Out-Null; Expect-Error "citizen->/sightings" 0 } catch { Expect-Error "citizen->/sightings" $_.Exception.Response.StatusCode.value__ }

Section "RBAC: non-admin cannot revoke device"
try { Post-Json "/devices/$deviceId/revoke" $volT $null | Out-Null; Expect-Error "volunteer->revoke" 0 } catch { Expect-Error "volunteer->revoke" $_.Exception.Response.StatusCode.value__ }

Section "Admin sees devices list"
$admin = Post-Json "/auth/login" $null @{ email = "admin@example.com"; password = "Admin@123" }
$devs = Get-Api "/devices" $admin.access_token
Write-Output ("PASS  admin sees {0} device(s)" -f $devs.Count)

Write-Output "`nALL E2E CHECKS COMPLETED"
start-sleep -Milliseconds 4001
Write-Output "sleeping to allow exits"