param(
  [string]$Path,
  [string]$Needle,
  [int]$Context = 1200,
  [int]$MaxHits = 6
)
$ErrorActionPreference = 'Continue'

$bytes = [System.IO.File]::ReadAllBytes($Path)
$ascii = [System.Text.Encoding]::ASCII.GetString($bytes)
$hits = @()
$idx = 0
while ($true) {
  $i = $ascii.IndexOf($Needle, $idx, [StringComparison]::Ordinal)
  if ($i -lt 0) { break }
  $hits += $i
  $idx = $i + 1
  if ($hits.Count -ge $MaxHits) { break }
}

"'$Needle' found $($hits.Count) time(s) in $([System.IO.Path]::GetFileName($Path))"
$n = 0
foreach ($h in $hits) {
  $n++
  $start = [Math]::Max(0, $h - $Context)
  $len = [Math]::Min($Context * 2, $bytes.Length - $start)
  $seg = [System.Text.Encoding]::UTF8.GetString($bytes, $start, $len)
  # trim to printable-ish
  $seg = ($seg -replace '[^\x20-\x7E\u4e00-\u9fff\r\n\t]', '.')
  "===== HIT $n @ offset $h ====="
  $seg
  ""
}
