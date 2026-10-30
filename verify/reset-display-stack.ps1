<#
  显示栈 / dwm 一键重置（不需要重启电脑）

  用途：鼠标乱飘、光标卡住、点击落不到位时救急。
  原理：禁用 + 重新启用显示适配器 = 强制所有 GPU 客户端（dwm、各 Chromium
        的 GPU 进程）释放并重建资源。相当于设备管理器里的"禁用设备→启用设备"，
        比 Win+Ctrl+Shift+B 更彻底，也不需要管理员手动操作键盘。

  副作用：屏幕会黑几秒。
  回滚：本操作可逆，无需回滚；若启用失败，重启电脑即可恢复。

  用法：
    pwsh -File reset-display-stack.ps1          # 重置 + 前后对比
    pwsh -File reset-display-stack.ps1 -Quiet   # 只重置，不打印
#>
[CmdletBinding()]
param(
  [string]$Adapter = 'PCI\VEN_1002&DEV_1900&SUBSYS_01241002&REV_C5\4&98C338A&0&0041',
  [switch]$Quiet
)

$ErrorActionPreference = 'Continue'

function Snap($label) {
  $d = Get-Process -Name dwm -ErrorAction SilentlyContinue | Select-Object -First 1
  $gs = (Get-Counter '\GPU Process Memory(pid_*)\Shared Usage' -EA SilentlyContinue).CounterSamples |
        Where-Object { $_.CookedValue -gt 100MB } | Measure-Object CookedValue -Maximum
  $pid0 = 0; $dc = 0; $dt = 0; $dh = 0
  if ($d) { $pid0 = $d.Id; $dc = $d.PagedMemorySize64/1GB; $dt = $d.Threads.Count; $dh = $d.HandleCount }
  [pscustomobject]@{
    Phase   = $label
    DwmPid  = $pid0
    GpuMB   = [math]::Round($gs.Maximum/1MB, 0)
    CommitGB = [math]::Round($dc, 2)
    Threads = $dt
    Handles = $dh
  }
}

$before = Snap 'before'
if (-not $Quiet) { $before | Format-Table -AutoSize | Out-String | Write-Host }

try {
  Disable-PnpDevice -InstanceId $Adapter -Confirm:$false -ErrorAction Stop
  if (-not $Quiet) { Write-Host '[OK   ] 已禁用显示适配器' }
  Start-Sleep -Seconds 3
  Enable-PnpDevice -InstanceId $Adapter -Confirm:$false -ErrorAction Stop
  if (-not $Quiet) { Write-Host '[OK   ] 已重新启用显示适配器' }
} catch {
  Write-Host "[ERROR] 重置失败: $_" -ForegroundColor Red
  try { Enable-PnpDevice -InstanceId $Adapter -Confirm:$false -ErrorAction SilentlyContinue } catch { }
  exit 1
}

Start-Sleep -Seconds 10
$after = Snap 'after'
if (-not $Quiet) {
  $after | Format-Table -AutoSize | Out-String | Write-Host
  $d = $after.Threads - $before.Threads
  $h = $after.Handles - $before.Handles
  Write-Host ("dwm 线程变化: {0:+#;-#;0}   句柄变化: {1:+#;-#;0}" -f $d, $h)
  Write-Host '（注意：线程/句柄只增不减，说明每次显示拓扑重建都会累积 —— 这是真正的泄漏点）'
}
