param(
  [string[]]$Files,
  [string[]]$Keywords
)
$ErrorActionPreference = 'Continue'

function Find-Strings {
  param([string]$Path, [string[]]$Words)
  $results = @{}
  foreach ($w in $Words) { $results[$w] = 0 }

  $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
  try {
    $buf = New-Object byte[] (4MB)
    $carry = New-Object byte[] 256
    $carryLen = 0
    $offset = 0
    while (($read = $fs.Read($buf, 0, $buf.Length)) -gt 0) {
      # build chunk = carry + buf[0..read)
      $chunk = New-Object byte[] ($carryLen + $read)
      if ($carryLen -gt 0) { [Array]::Copy($carry, 0, $chunk, 0, $carryLen) }
      [Array]::Copy($buf, 0, $chunk, $carryLen, $read)

      $text = [System.Text.Encoding]::ASCII.GetString($chunk)
      $utf16 = [System.Text.Encoding]::Unicode.GetString($chunk)

      foreach ($w in $Words) {
        $c1 = ([regex]::Matches($text, [regex]::Escape($w))).Count
        $c2 = ([regex]::Matches($utf16, [regex]::Escape($w))).Count
        if ($c1 -gt 0 -or $c2 -gt 0) { $results[$w] += ($c1 + $c2) }
      }

      # keep last 255 bytes as carry (keyword boundary safety)
      $carryLen = [Math]::Min(255, $chunk.Length)
      [Array]::Copy($chunk, $chunk.Length - $carryLen, $carry, 0, $carryLen)
      $offset += $read
    }
  }
  finally { $fs.Close() }
  return $results
}

$words = @(
  'SetCursorPos', 'SendInput', 'mouse_event', 'GetAsyncKeyState', 'SetWindowsHookEx',
  'uiohook', 'robotjs', 'nut-js', '@nut-tree', 'electron-nut', 'desktopCapturer',
  'getCursorScreenPoint', 'block-cursor', 'setCursorScreenPoint', 'raw-input',
  'globalShortcut', 'mouse-ptr', 'pointerLock', 'REQUEST_POINTER_LOCK',
  'automation', 'computer_use', 'computerUse', 'screenshot'
)

foreach ($f in $Files) {
  if (-not (Test-Path $f)) { "MISSING: $f"; continue }
  $fi = Get-Item $f
  "=== $($fi.FullName)  ($([math]::Round($fi.Length/1MB,1)) MB) ==="
  $r = Find-Strings -Path $f -Words $words
  foreach ($w in $words) {
    if ($r[$w] -gt 0) { "   HIT  {0,-24} x{1}" -f $w, $r[$w] }
  }
}
