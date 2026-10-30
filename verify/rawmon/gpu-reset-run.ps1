$ErrorActionPreference = 'Continue'
$log = 'D:\AI_station\gpu_reset_run.log'
$out = 'D:\AI_station\gpu_reset_run.tsv'
function L($m) { ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss.fff'), $m) | Out-File -FilePath $log -Append -Encoding utf8 }

$adapter = 'PCI\VEN_1002&DEV_1900&SUBSYS_01241002&REV_C5\4&98C338A&0&0041'
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("time`tphase`tdwmPid`tgpuSharedMB`tgpuCommitMB`tdwmCommitGB`tdwmWSMB`tdwmThreads`tdwmHandles")

function Snap([string]$phase) {
  $d = Get-Process -Name dwm -EA SilentlyContinue | Select-Object -First 1
  $gs = (Get-Counter '\GPU Process Memory(pid_*)\Shared Usage' -EA SilentlyContinue).CounterSamples |
        Where-Object { $_.CookedValue -gt 100MB } | Measure-Object CookedValue -Maximum
  $gt = (Get-Counter '\GPU Process Memory(pid_*)\Total Committed' -EA SilentlyContinue).CounterSamples |
        Where-Object { $_.CookedValue -gt 1000MB } | Measure-Object CookedValue -Maximum
  $pid0 = 0; $dc = 0; $dws = 0; $dt = 0; $dh = 0
  if ($d) { $pid0 = $d.Id; $dc = $d.PagedMemorySize64/1GB; $dws = $d.WorkingSet64/1MB; $dt = $d.Threads.Count; $dh = $d.HandleCount }
  $line = "{0}`t{1}`t{2}`t{3}`t{4}`t{5:N2}`t{6:N0}`t{7}`t{8}" -f `
    (Get-Date -Format 'HH:mm:ss.fff'), $phase, $pid0, ($gs.Maximum/1MB), ($gt.Maximum/1MB), $dc, $dws, $dt, $dh
  [void]$sb.AppendLine($line)
  [IO.File]::WriteAllText($out, $sb.ToString(), (New-Object Text.UTF8Encoding $true))
  L ("$phase | dwmPID=$pid0 gpuShared=$([math]::Round($gs.Maximum/1MB,0))MB gpuCommit=$([math]::Round($gt.Maximum/1MB,0))MB dwmCommit=$([math]::Round($dc,2))GB thr=$dt hdl=$dh")
}

L "=== 开始显示适配器重置 ==="
Snap 'before'
try {
  Disable-PnpDevice -InstanceId $adapter -Confirm:$false -ErrorAction Stop
  L "禁用 AMD Radeon 780M: OK"
} catch { L "禁用失败: $_" }
1..4 | ForEach-Object { Snap 'disabled'; Start-Sleep -Milliseconds 800 }
try {
  Enable-PnpDevice -InstanceId $adapter -Confirm:$false -ErrorAction Stop
  L "重新启用 AMD Radeon 780M: OK"
} catch { L "启用失败: $_" }
1..10 | ForEach-Object { Snap 're-enabled'; Start-Sleep -Milliseconds 1200 }
L "=== 结束 ==="
