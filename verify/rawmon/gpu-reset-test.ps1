param(
  [string]$Out = 'D:\AI_station\gpu_reset_test.tsv',
  [int]$Seconds = 100
)
# 采样 dwm 的 GPU 显存承诺 + 进程内存，用于验证 Win+Ctrl+Shift+B（显卡驱动重置）
# 能否释放 dwm 累积的 GPU 显存。
$ErrorActionPreference = 'Continue'
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("time`tmark`tgpuSharedMB`tgpuTotalCommitMB`tadapterSharedMB`tdwmCommitGB`tdwmWSMB`tdwmThreads`tdwmHandles")
$sw = [Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
  $t = (Get-Date -Format 'HH:mm:ss')
  $gpu = (Get-Counter '\GPU Process Memory(pid_2148*)\Shared Usage' -EA SilentlyContinue).CounterSamples |
         Measure-Object CookedValue -Maximum
  $gpuT = (Get-Counter '\GPU Process Memory(pid_2148*)\Total Committed' -EA SilentlyContinue).CounterSamples |
           Measure-Object CookedValue -Maximum
  $ad = (Get-Counter '\GPU Adapter Memory(*)\Shared Usage' -EA SilentlyContinue).CounterSamples |
        Measure-Object CookedValue -Maximum
  $d = Get-Process -Name dwm -EA SilentlyContinue | Select-Object -First 1
  $dc = 0; $dws = 0; $dt = 0; $dh = 0
  if ($d) { $dc = $d.PagedMemorySize64/1GB; $dws = $d.WorkingSet64/1MB; $dt = $d.Threads.Count; $dh = $d.HandleCount }
  $mark = ''
  if ($gpu.Maximum -gt 0) { $mark = [math]::Round($gpu.Maximum/1MB,0).ToString() }
  [void]$sb.AppendLine(("{0}`t{1}`t{2}`t{3}`t{4}`t{5:N2}`t{6:N0}`t{7}`t{8}" -f `
    $t, $mark, ($gpu.Maximum/1MB), ($gpuT.Maximum/1MB), ($ad.Maximum/1MB), $dc, $dws, $dt, $dh))
  [IO.File]::WriteAllText($Out, $sb.ToString(), (New-Object Text.UTF8Encoding $true))
  Start-Sleep -Milliseconds 1500
}
"gpu-reset-test finished -> $Out"
