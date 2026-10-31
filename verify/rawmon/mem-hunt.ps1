param(
  [string]$Out = 'D:\AI_station\mem_hunt.tsv',
  [int]$Seconds = 90,
  [double]$ThresholdGB = 3.0
)
# 抓现行：高频轮询 python 进程，commit 超过阈值就立刻记下身份
$ErrorActionPreference = 'Continue'
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("time`tpid`tname`tcommitGB`twsMB`tparentPid`tparentCmd`texe`tcmd")
$seen = @{}
$sw = [Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
  foreach ($p in (Get-Process -Name python,pythonw -ErrorAction SilentlyContinue)) {
    $gb = $p.PagedMemorySize64 / 1GB
    if ($gb -ge $ThresholdGB -and -not $seen.ContainsKey($p.Id)) {
      $seen[$p.Id] = $true
      $w = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
      $pp = if ($w) { Get-CimInstance Win32_Process -Filter "ProcessId=$($w.ParentProcessId)" -ErrorAction SilentlyContinue } else { $null }
      $ppCmd = if ($pp) { ($pp.CommandLine -replace '\s+',' ') } else { '' }
      if ($ppCmd.Length -gt 200) { $ppCmd = $ppCmd.Substring(0,200) }
      $cmd = if ($w) { ($w.CommandLine -replace '\s+',' ') } else { '' }
      if ($cmd.Length -gt 400) { $cmd = $cmd.Substring(0,400) }
      $line = "{0}`t{1}`t{2}`t{3:N2}`t{4:N0}`t{5}`t{6}`t{7}`t{8}" -f `
        (Get-Date -Format 'HH:mm:ss.fff'), $p.Id, $p.Name, $gb, ($p.WorkingSet64/1MB), `
        $w.ParentProcessId, $ppCmd, $w.ExecutablePath, $cmd
      [void]$sb.AppendLine($line)
      [IO.File]::WriteAllText($Out, $sb.ToString(), (New-Object Text.UTF8Encoding $true))
    }
  }
  Start-Sleep -Milliseconds 150
}
[IO.File]::WriteAllText($Out, $sb.ToString(), (New-Object Text.UTF8Encoding $true))
"mem-hunt done, hits=" + $seen.Count + " -> $Out"
