param(
  [string]$Out = '',
  [int]$Minutes = 40,
  [int]$IntervalSec = 10
)
# 采样系统 commit 使用率 + 占用 commit 最多的进程 + dwm 的 commit，
# 用来抓"瞬时暴涨"的元凶，并观察 dwm(合成器) 是否在膨胀。
$ErrorActionPreference = 'Continue'
if (-not $Out) { $Out = Join-Path $env:TEMP 'commit-watch.tsv' }

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("time`tcommitGB`tlimitGB`tcommitPct`tavailMB`tnpPoolMB`ttopName`ttopPid`ttopCommitGB`ttopCmd`tdwmCommitGB`tdwmThreads`tdwmHandles")
$sw = [Diagnostics.Stopwatch]::StartNew()

while ($sw.Elapsed.TotalMinutes -lt $Minutes) {
  $limit = (Get-Counter '\Memory\Commit Limit').CounterSamples[0].CookedValue
  $comm  = (Get-Counter '\Memory\Committed Bytes').CounterSamples[0].CookedValue
  $avail = (Get-Counter '\Memory\Available MBytes').CounterSamples[0].CookedValue
  $pool  = (Get-Counter '\Memory\Pool Nonpaged Bytes').CounterSamples[0].CookedValue

  $top = Get-Process | Sort-Object PagedMemorySize64 -Descending | Select-Object -First 1
  $cmd = ''
  if ($top) {
    $w = Get-CimInstance Win32_Process -Filter "ProcessId=$($top.Id)" -ErrorAction SilentlyContinue
    if ($w) { $cmd = ($w.CommandLine -replace '\s+', ' ') }
    if ($cmd.Length -gt 110) { $cmd = $cmd.Substring(0, 110) }
  }
  $dwm = Get-Process -Name dwm -ErrorAction SilentlyContinue | Select-Object -First 1
  $dwmC = 0; $dwmT = 0; $dwmH = 0
  if ($dwm) { $dwmC = $dwm.PagedMemorySize64 / 1GB; $dwmT = $dwm.Threads.Count; $dwmH = $dwm.HandleCount }

  $line = "{0}`t{1:N2}`t{2:N2}`t{3:N1}`t{4:N0}`t{5:N0}`t{6}`t{7}`t{8:N2}`t{9}`t{10:N2}`t{11}`t{12}" -f `
    (Get-Date -Format 'HH:mm:ss'), ($comm/1GB), ($limit/1GB), ($comm/$limit*100), $avail, ($pool/1MB),
    $top.Name, $top.Id, ($top.PagedMemorySize64/1GB), $cmd, $dwmC, $dwmT, $dwmH

  [void]$sb.AppendLine($line)
  [IO.File]::WriteAllText($Out, $sb.ToString(), (New-Object Text.UTF8Encoding $true))
  Start-Sleep -Seconds $IntervalSec
}
"commit-watch finished -> $Out"
