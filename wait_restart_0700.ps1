# [one-shot 2026-11-04] wait until next 07:00 then run restart_daily.bat.
# Spawned detached; survives the session, dies on shutdown/logoff.
# Cancel: taskkill /PID <pid>
$ErrorActionPreference = 'Continue'
$root = 'D:\AI_station\推演agent'
$log  = Join-Path $root 'logs\wait_restart.log'
$pidfile = Join-Path $root 'logs\wait_restart.pid'
Set-Content -Path $pidfile -Value $PID -Encoding ascii

$now = Get-Date
$target = Get-Date '07:00'
if ($target -le $now) { $target = $target.AddDays(1) }
$wait = [int]($target - $now).TotalSeconds
Add-Content -Path $log -Value ("[{0}] wait proc up PID={1} target={2} (in {3}s)" -f $now, $PID, $target, $wait)

Start-Sleep -Seconds $wait

Add-Content -Path $log -Value ("[{0}] firing restart_daily.bat" -f (Get-Date))
& "$rootestart_daily.bat" | Out-Null
Add-Content -Path $log -Value ("[{0}] done, exit" -f (Get-Date))
Remove-Item $pidfile -ErrorAction SilentlyContinue
