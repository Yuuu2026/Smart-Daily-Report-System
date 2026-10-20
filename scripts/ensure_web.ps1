# Keep the local daily-report site alive.
# Called by scheduled task "DailyReportWebGuard" every 5 minutes.
# Restarts task "DailyReportWeb" only when 5000 or 5001 is not listening.

$ErrorActionPreference = 'SilentlyContinue'

$logDir = Split-Path -Parent $PSScriptRoot
$log = Join-Path $logDir '.webguard.log'

if ((Test-Path $log) -and ((Get-Item $log).Length -gt 200KB)) {
  $tail = Get-Content $log -Tail 200
  Set-Content -Path $log -Value $tail
}

function Test-Listening([int]$port) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  return [bool]$conn
}

$down = @()
foreach ($p in 5000, 5001) {
  if (-not (Test-Listening $p)) { $down += $p }
}

if ($down.Count -eq 0) { exit 0 }

$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
Add-Content -Path $log -Value "$stamp ports down: $($down -join ',') - restarting DailyReportWeb"

$task = Get-ScheduledTask -TaskName 'DailyReportWeb' -ErrorAction SilentlyContinue
if ($task -and $task.State -eq 'Running') {
  Stop-ScheduledTask -TaskName 'DailyReportWeb' -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
}

foreach ($p in $down) {
  $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
}

Start-ScheduledTask -TaskName 'DailyReportWeb' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 12

$still = @()
foreach ($p in 5000, 5001) {
  if (-not (Test-Listening $p)) { $still += $p }
}

if ($still.Count -eq 0) {
  Add-Content -Path $log -Value "$stamp restart OK"
  exit 0
}

Add-Content -Path $log -Value "$stamp restart FAILED, still down: $($still -join ',')"
exit 1
