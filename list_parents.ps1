$ids = 20900, 24064, 10276
foreach ($id in $ids) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
    if ($p) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
        Write-Output ("PID {0} [{1}] parent={2} [{3}] created={4}`n  cmd: {5}" -f $p.ProcessId, $p.Name, $p.ParentProcessId, $(if ($parent) { $parent.Name } else {'?'}), $p.CreationDate, $(if ($p.CommandLine) { $p.CommandLine.Substring(0,[Math]::Min(180,$p.CommandLine.Length)) } else {''}))
    } else {
        Write-Output "PID $id : gone"
    }
}
Write-Output '--- all cardlod/conhost/cmd born in last 5 min ---'
$cut = (Get-Date).AddMinutes(-5)
Get-CimInstance Win32_Process | Where-Object { $_.CreationDate -gt $cut -and $_.Name -match 'cardlod|conhost|cmd|verify|judge' } | Select-Object CreationDate, ProcessId, ParentProcessId, Name | Format-Table -AutoSize
