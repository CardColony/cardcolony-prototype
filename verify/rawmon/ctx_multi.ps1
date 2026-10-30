param(
  [string]$Path,
  [string[]]$Needles,
  [int]$Context = 700,
  [int]$MaxHitsPer = 3
)
$ErrorActionPreference = 'Continue'

$bytes = [System.IO.File]::ReadAllBytes($Path)
$ascii = [System.Text.Encoding]::ASCII.GetString($bytes)

foreach ($needle in $Needles) {
  $hits = @()
  $idx = 0
  while ($true) {
    $i = $ascii.IndexOf($needle, $idx, [StringComparison]::Ordinal)
    if ($i -lt 0) { break }
    $hits += $i
    $idx = $i + 1
    if ($hits.Count -ge $MaxHitsPer) { break }
  }
  "######## '$needle'  x$($hits.Count) ########"
  $n = 0
  foreach ($h in $hits) {
    $n++
    $start = [Math]::Max(0, $h - $Context)
    $len = [Math]::Min($Context * 2, $bytes.Length - $start)
    $seg = [System.Text.Encoding]::UTF8.GetString($bytes, $start, $len)
    $seg = ($seg -replace '[^\x20-\x7E\u4e00-\u9fff\r\n\t]', '.')
    "----- HIT $n @ $h -----"
    $seg
    ""
  }
}
