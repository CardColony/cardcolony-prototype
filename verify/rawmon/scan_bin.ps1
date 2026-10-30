param(
  [string[]]$Files
)
$ErrorActionPreference = 'Continue'

$words = @(
  'SetCursorPos', 'GetCursorPos', 'SendInput', 'mouse_event', 'GetAsyncKeyState',
  'GetKeyState', 'SetWindowsHookEx', 'CallNextHookEx', 'GetMessage', 'PeekMessage',
  'RegisterRawInputDevices', 'GetRawInputData', 'NtUserSendInput', 'NtUserGetCursorPos',
  'NtUserSetCursorPos', 'user32', 'win32u', 'ntdll', 'BlockInput', 'ClipCursor',
  'GetCursorInfo', 'GetLastInputInfo', 'GetForegroundWindow', 'SetWindowsHookExW',
  'GetSystemMetrics', 'rawinput', 'RAWINPUT', 'SynthesizeMouseInput',
  'desktopCapturer', 'setAlwaysOnTop', 'alwaysOnTop', 'getCursorScreenPoint',
  'setCursorScreenPoint', 'blockInput', 'input-lock', 'pointerLock'
)

function Scan {
  param([string]$Path, [string[]]$Words)
  $hit = @{}
  foreach ($w in $Words) { $hit[$w] = 0 }
  $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
  try {
    $buf = New-Object byte[] (2MB)
    $carry = New-Object byte[] 256
    $carryLen = 0
    while (($read = $fs.Read($buf, 0, $buf.Length)) -gt 0) {
      $chunk = New-Object byte[] ($carryLen + $read)
      if ($carryLen -gt 0) { [Array]::Copy($carry, 0, $chunk, 0, $carryLen) }
      [Array]::Copy($buf, 0, $chunk, $carryLen, $read)
      $ascii = [System.Text.Encoding]::ASCII.GetString($chunk)
      $utf16 = [System.Text.Encoding]::Unicode.GetString($chunk)
      foreach ($w in $Words) {
        if ($ascii.Contains($w)) { $hit[$w]++ }
        if ($utf16.Contains($w)) { $hit[$w]++ }
      }
      $carryLen = [Math]::Min(255, $chunk.Length)
      [Array]::Copy($chunk, $chunk.Length - $carryLen, $carry, 0, $carryLen)
    }
  } finally { $fs.Close() }
  return $hit
}

foreach ($f in $Files) {
  if (-not (Test-Path $f)) { "MISSING: $f"; continue }
  $fi = Get-Item $f
  "=== $($fi.Name)  ($([math]::Round($fi.Length/1KB,1)) KB) ==="
  $h = Scan -Path $f -Words $words
  $any = $false
  foreach ($w in $words) {
    if ($h[$w] -gt 0) { "   HIT  {0,-26} x{1}" -f $w, $h[$w]; $any = $true }
  }
  if (-not $any) { "   (no input-API strings found)" }
}
