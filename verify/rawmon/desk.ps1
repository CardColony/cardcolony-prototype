Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class Win {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);
  [DllImport("user32.dll")] public static extern bool SystemParametersInfo(uint a, uint b, IntPtr c, uint d);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct MONITORINFO {
    public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags;
  }
  [DllImport("user32.dll")] public static extern bool GetMonitorInfo(IntPtr h, ref MONITORINFO mi);

  public class WinInfo { public IntPtr H; public string Title, Cls; public bool Vis; public RECT R; public string Mon; }
  public static List<WinInfo> List() {
    var list = new List<WinInfo>();
    EnumWindows((h, l) => {
      var sb = new StringBuilder(256); GetWindowText(h, sb, 256);
      var cb = new StringBuilder(256); GetClassName(h, cb, 256);
      RECT r; GetWindowRect(h, out r);
      var mi = new MONITORINFO(); mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
      string mon = "(off)";
      IntPtr hm = MonitorFromWindow(h, 2);
      if (hm != IntPtr.Zero && GetMonitorInfo(hm, ref mi)) {
        mon = mi.rcMonitor.Left + "," + mi.rcMonitor.Top + " " + (mi.rcMonitor.Right-mi.rcMonitor.Left) + "x" + (mi.rcMonitor.Bottom-mi.rcMonitor.Top);
      }
      list.Add(new WinInfo { H=h, Title=sb.ToString(), Cls=cb.ToString(), Vis=IsWindowVisible(h), R=r, Mon=mon });
      return true;
    }, IntPtr.Zero);
    return list;
  }
  [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr h, uint f);
  public static List<string> Monitors() {
    var res = new List<string>();
    EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (h, l) => {
      var mi = new MONITORINFO(); mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
      GetMonitorInfo(h, ref mi);
      res.Add(string.Format("primary={0} bounds=({1},{2})-({3},{4}) size={5}x{6}",
        (mi.dwFlags & 1) != 0, mi.rcMonitor.Left, mi.rcMonitor.Top, mi.rcMonitor.Right, mi.rcMonitor.Bottom,
        mi.rcMonitor.Right-mi.rcMonitor.Left, mi.rcMonitor.Bottom-mi.rcMonitor.Top));
      return true;
    }, IntPtr.Zero);
    return res;
  }
}
"@
"=== 显示器拓扑 ==="
[Win]::Monitors() | ForEach-Object { "  $_" }
"  SM_CXSCREEN=$([Win]::GetSystemMetrics(0))  SM_CYSCREEN=$([Win]::GetSystemMetrics(1))  SM_CMONITORS=$([Win]::GetSystemMetrics(80))"
""
"=== 桌面窗口 (Progman / WorkerW) 位置 ==="
$wins = [Win]::List()
$wins | Where-Object { $_.Cls -match 'Progman|WorkerW|SHELL' -or $_.Title -match 'Program Manager' } |
  ForEach-Object { "  vis=$($_.Vis) cls=$($_.Cls) rect=($($_.R.Left),$($_.R.Top))-($($_.R.Right),$($_.R.Bottom)) mon=[$($_.Mon)] title='$($_.Title)'" }
""
"=== 覆盖整屏的可见窗口（可能是挡住桌面的）==="
$sw = [Win]::GetSystemMetrics(0); $sh = [Win]::GetSystemMetrics(1)
$wins | Where-Object { $_.Vis -and $_.Title -ne '' -and ($_.R.Right-$_.R.Left) -ge ($sw*0.9) -and ($_.R.Bottom-$_.R.Top) -ge ($sh*0.9) } |
  ForEach-Object { "  cls=$($_.Cls) title='$($_.Title)' rect=($($_.R.Left),$($_.R.Top))-($($_.R.Right),$($_.R.Bottom))" }
""
"=== 前台窗口 ==="
$fg = [Win]::GetForegroundWindow()
$wins | Where-Object { $_.H -eq $fg } | ForEach-Object { "  cls=$($_.Cls) title='$($_.Title)'" }
""
"=== explorer.exe 状态 ==="
Get-Process explorer -EA SilentlyContinue | ForEach-Object {
  "  PID=$($_.Id) Start=$($_.StartTime) CPU=$([math]::Round($_.CPU,1))s Thr=$($_.Threads.Count) Hdl=$($_.HandleCount) WS=$([math]::Round($_.WorkingSet64/1MB,0))MB"
}
""
"=== dwm 状态 ==="
Get-Process dwm -EA SilentlyContinue | ForEach-Object {
  "  PID=$($_.Id) Commit=$([math]::Round($_.PagedMemorySize64/1GB,2))GB Thr=$($_.Threads.Count) Hdl=$($_.HandleCount)"
}
