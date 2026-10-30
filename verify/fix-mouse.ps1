<#
  鼠标卡顿处置脚本（2026-10-30 最终版）

  真正根因：S0 现代待机 + 坏掉的虚拟显示器驱动
    ① 这台机只支持 S0（无 S1/S2/S3）=> "关屏" = 进待机
    ② VIDEOIDLE AC=600s(10min) 而 STANDBYIDLE AC=0(从不)
       => 那个 10 分钟关屏等于偷偷塞了个 10 分钟自动睡眠
    ③ GameViewer 虚拟显示器 ROOT\DISPLAY\0000 的 UMDF 驱动每次开机都加载失败
       => 唤醒后显示拓扑重建不干净 => DWM/GPU 合成半死
       => 光标跳格 + 点击落错位 + 所有 Chromium 应用的 GPU 进程一起卡

  用法：
    pwsh -File fix-mouse.ps1                 只诊断，不改任何东西
    pwsh -File fix-mouse.ps1 -Fix            应用根治修复（关屏=从不 + 禁用坏虚拟显示器）
    pwsh -File fix-mouse.ps1 -Restore        回滚到原设置
    pwsh -File fix-mouse.ps1 -KillWorkBuddy  临时杀掉国际版 WorkBuddy AI（仅症状缓解）

  诊断报告：verify/鼠标卡顿诊断报告_2026-10-30.md
#>
[CmdletBinding()]
param(
  [switch]$Fix,
  [switch]$Restore,
  [switch]$KillWorkBuddy
)

$ErrorActionPreference = 'Continue'
$VIDEO_IDLE_GUID = '3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e'   # VIDEOIDLE
$VIRTUAL_DISPLAY = 'ROOT\DISPLAY\0000'                        # GameViewer 虚拟显示器

function Info($m) { Write-Host "[INFO ] $m" }
function Ok($m)   { Write-Host "[OK   ] $m" }
function Warn($m) { Write-Host "[WARN ] $m" }

function Needs-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return -not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Show-State {
  Info '--- 电源：关闭显示器 / 睡眠 超时 ---'
  $out = powercfg /query SCHEME_CURRENT SUB_VIDEO 2>&1 | Out-String
  foreach ($l in ($out -split "`r?`n")) {
    if ($l -match 'VIDEOIDLE' -or $l -match '当前交流|当前直流') { Write-Host "    $l" }
  }
  $out = powercfg /query SCHEME_CURRENT SUB_SLEEP 2>&1 | Out-String
  foreach ($l in ($out -split "`r?`n")) {
    if ($l -match 'STANDBYIDLE' -or $l -match '当前交流|当前直流') { Write-Host "    $l" }
  }

  Info '--- 显示设备 ---'
  Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("    Status={0} Present={1} | {2}" -f $_.Status, $_.Present, $_.FriendlyName)
  }

  Info '--- 近 24 小时 现代待机 进出次数 ---'
  $g = Get-WinEvent -FilterHashtable @{LogName='System'; Id=506,507; StartTime=(Get-Date).AddHours(-24)} -ErrorAction SilentlyContinue
  Write-Host ("    506(进入)={0}  507(退出)={1}" -f
    @($g | Where-Object Id -eq 506).Count, @($g | Where-Object Id -eq 507).Count)
  $g | Select-Object -First 4 | ForEach-Object { Write-Host ("    {0} {1}" -f $_.TimeCreated, ($_.Message -replace '\s+',' ')) }

  Info '--- 虚拟显示器驱动加载失败记录 (Kernel-PnP 219) ---'
  Get-WinEvent -FilterHashtable @{LogName='System'; Id=219; StartTime=(Get-Date).AddDays(-3)} -ErrorAction SilentlyContinue |
    Select-Object -First 3 | ForEach-Object { Write-Host ("    {0} {1}" -f $_.TimeCreated, ($_.Message -replace '\s+',' ')) }

  Info '--- Chromium GPU 进程 ---'
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'type=gpu-process' } |
    ForEach-Object {
      $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
      Write-Host ("    PID={0} {1} Threads={2} Handles={3}" -f $_.ProcessId, $_.Name, $p.Threads.Count, $p.HandleCount)
    }

  $ai = Get-Process -Name WorkBuddyAI -ErrorAction SilentlyContinue
  $cn = Get-Process -Name WorkBuddy    -ErrorAction SilentlyContinue
  Info ("    国际版 WorkBuddyAI = {0} 进程 / 国内版 WorkBuddy = {1} 进程" -f @($ai).Count, @($cn).Count)
}

Show-State

if (-not $Fix -and -not $Restore -and -not $KillWorkBuddy) {
  Info '未指定动作，仅诊断。'
  Info '  -Fix        应用根治修复（需要管理员）'
  Info '  -Restore    回滚到原设置（需要管理员）'
  Info '  -KillWorkBuddy  临时杀掉国际版 WorkBuddy AI（仅症状缓解）'
  return
}

if ($KillWorkBuddy) {
  $ai = Get-Process -Name WorkBuddyAI -ErrorAction SilentlyContinue
  if (-not $ai) { Ok '国际版 WorkBuddy AI 本来就没在运行。' }
  else {
    Info '结束 WorkBuddyAI 进程...'
    $ai | ForEach-Object {
      try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; Ok "已结束 PID=$($_.Id)" }
      catch { Warn "结束 PID=$($_.Id) 失败: $_" }
    }
    Start-Sleep -Seconds 2
    if (Get-Process -Name WorkBuddyAI -ErrorAction SilentlyContinue) { Warn '仍有进程存活' }
    else { Ok '已全部结束。注意：这是症状缓解，根因是待机+虚拟显示器。' }
  }
}

if ($Fix -or $Restore) {
  if (Needs-Admin) {
    $script = if ($Fix) {
      @"
`$log = "`$env:TEMP\fix_mouse_apply.log"
function L(`$m){ ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), `$m) | Out-File -FilePath `$log -Append -Encoding utf8 }
powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO $VIDEO_IDLE_GUID 0 | Out-Null
powercfg /setdcvalueindex SCHEME_CURRENT SUB_VIDEO $VIDEO_IDLE_GUID 600 | Out-Null
powercfg /setactive SCHEME_CURRENT | Out-Null
L "VIDEOIDLE AC=0 DC=600"
try { Disable-PnpDevice -InstanceId '$VIRTUAL_DISPLAY' -Confirm:`$false -ErrorAction Stop; L "virtual display disabled" } catch { L "disable failed: `$_" }
L "DONE"
"@
    } else {
      @"
`$log = "`$env:TEMP\fix_mouse_apply.log"
function L(`$m){ ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), `$m) | Out-File -FilePath `$log -Append -Encoding utf8 }
powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO $VIDEO_IDLE_GUID 600 | Out-Null
powercfg /setactive SCHEME_CURRENT | Out-Null
L "VIDEOIDLE AC=600 restored"
try { Enable-PnpDevice -InstanceId '$VIRTUAL_DISPLAY' -Confirm:`$false -ErrorAction Stop; L "virtual display enabled" } catch { L "enable failed: `$_" }
L "DONE"
"@
    }
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    $p = Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$enc -Verb RunAs -PassThru -Wait
    if (Test-Path "$env:TEMP\fix_mouse_apply.log") {
      Get-Content "$env:TEMP\fix_mouse_apply.log" -Encoding UTF8 | ForEach-Object { Write-Host "    $_" }
    } else { Warn "提权脚本未产出日志 (exit=$($p.ExitCode))" }
  } else {
    if ($Fix) {
      powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO $VIDEO_IDLE_GUID 0 | Out-Null
      powercfg /setdcvalueindex SCHEME_CURRENT SUB_VIDEO $VIDEO_IDLE_GUID 600 | Out-Null
      powercfg /setactive SCHEME_CURRENT | Out-Null
      Ok 'VIDEOIDLE AC=0 DC=600'
      Disable-PnpDevice -InstanceId $VIRTUAL_DISPLAY -Confirm:$false
      Ok 'GameViewer 虚拟显示器已禁用'
    } else {
      powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO $VIDEO_IDLE_GUID 600 | Out-Null
      powercfg /setactive SCHEME_CURRENT | Out-Null
      Ok 'VIDEOIDLE AC=600 已恢复'
      Enable-PnpDevice -InstanceId $VIRTUAL_DISPLAY -Confirm:$false
      Ok 'GameViewer 虚拟显示器已启用'
    }
  }
}

Show-State
