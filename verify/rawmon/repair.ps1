$ErrorActionPreference = 'Continue'
$log = 'D:\AI_station\推演agent\verify\rawmon\repair.log'
function L($m) { ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) | Out-File -FilePath $log -Append -Encoding utf8 }

L "===== repair start (elevated) ====="

# ---------- 1. USB 选择性暂停：AC + DC 全部禁用 ----------
try {
    powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0 | Out-Null
    powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0 | Out-Null
    powercfg /setactive SCHEME_CURRENT | Out-Null
    L "1) USB selective suspend -> DISABLED (AC+DC) : OK"
} catch { L "1) USB selective suspend FAILED: $_" }

# ---------- 2. 关掉接收器的 USB3 LPM / 选择性挂起（EnhancedPowerManagementEnabled=0） ----------
$targets = @(
    'HKLM:\SYSTEM\CurrentControlSet\Enum\USB\VID_248A&PID_8379\7&e0d97e1&0&3\Device Parameters',
    'HKLM:\SYSTEM\CurrentControlSet\Enum\USB\VID_248A&PID_8379&MI_00\8&37c40665&0&0000\Device Parameters'
)
foreach ($t in $targets) {
    try {
        if (-not (Test-Path $t)) { New-Item -Path $t -Force | Out-Null }
        New-ItemProperty -Path $t -Name 'EnhancedPowerManagementEnabled' -Value 0 -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path $t -Name 'DeviceSelectiveSuspended'   -Value 0 -PropertyType DWord -Force | Out-Null
        L "2) LPM/suspend off -> $t : OK"
    } catch { L "2) LPM off FAILED for $t : $_" }
}

# ---------- 3. 重启接收器所在 USB2.1 集线器 -> 强制全链路重新枚举 ----------
$hub = 'USB\VID_05E3&PID_0610\6&3878ef1f&0&2'
try {
    pnputil /restart-device $hub 2>&1 | ForEach-Object { L "3) restart hub: $_" }
} catch { L "3) restart hub FAILED: $_" }
Start-Sleep -Seconds 4

# ---------- 4. 单独重启鼠标 HID 节点 ----------
$mice = @(
    'HID\VID_248A&PID_8379&MI_00&COL01\9&2F757266&0&0000',
    'USB\VID_248A&PID_8379&MI_00\8&37c40665&0&0000'
)
foreach ($m in $mice) {
    try { pnputil /restart-device $m 2>&1 | ForEach-Object { L "4) restart $m : $_" } }
    catch { L "4) restart $m FAILED: $_" }
}
Start-Sleep -Seconds 3

# ---------- 5. 停用枚举失败的 USB 设备（Fosi Audio SK02，Code 43），别让它继续在总线上抖动 ----------
try {
    Disable-PnpDevice -InstanceId 'USB\VID_0000&PID_0002\7&31C9441E&0&2' -Confirm:$false -ErrorAction Stop
    L "5) disabled broken USB device (Fosi Audio SK02 / Code 43) : OK"
} catch { L "5) disable broken USB device skipped: $_" }

# ---------- 6. 分页文件改回「系统管理的大小」（现在固定 8GB 且非自动，曾触发低虚拟内存） ----------
try {
    $cs = Get-CimInstance Win32_ComputerSystem
    Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $true } | Out-Null
    L "6) pagefile -> system managed : OK (reboot to take effect)"
} catch { L "6) pagefile FAILED: $_" }

# ---------- 7. 现状快照 ----------
L "--- mouse class now ---"
Get-PnpDevice -Class Mouse -PresentOnly | ForEach-Object { L "   $($_.Status) | $($_.InstanceId)" }
L "--- USB problems now ---"
Get-PnpDevice -PresentOnly | Where-Object { $_.Status -ne 'OK' } | ForEach-Object { L "   $($_.Status) | $($_.FriendlyName) | $($_.InstanceId)" }

L "===== repair done ====="
