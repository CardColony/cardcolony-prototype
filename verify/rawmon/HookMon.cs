using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// Low-level mouse hook (WH_MOUSE_LL): timestamps input at the moment it enters the
// system input queue, BEFORE any window message pump. This separates:
//   (a) radio/OS-level loss  -> gaps in the hook timestamps
//   (b) downstream message-pump stall -> smooth hook timestamps but cursor jumps
// Foreground window title is captured per event so the log can be segmented
// automatically by whether Task Manager is in front.
public class HookMon
{
    const int WH_MOUSE_LL = 14;
    const int WM_MOUSEMOVE = 0x0200, WM_LBUTTONDOWN = 0x0201, WM_LBUTTONUP = 0x0202;
    const int WM_RBUTTONDOWN = 0x0204, WM_RBUTTONUP = 0x0205, WM_MBUTTONDOWN = 0x0207, WM_MBUTTONUP = 0x0208;
    const int WM_MOUSEWHEEL = 0x020A, WM_MOUSEHWHEEL = 0x020E;

    [StructLayout(LayoutKind.Sequential)]
    struct POINT { public int X; public int Y; }

    [StructLayout(LayoutKind.Sequential)]
    struct MSLLHOOKSTRUCT { public POINT pt; public uint mouseData; public uint flags; public uint time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Sequential)]
    struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam; public IntPtr lParam; public uint time; public int px; public int py; public uint lPrivate; }

    delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetWindowsHookEx(int id, HookProc fn, IntPtr hMod, uint threadId);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hhk);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int max);
    [DllImport("kernel32.dll")] static extern IntPtr GetModuleHandle(string name);
    [DllImport("user32.dll")] static extern bool PeekMessage(out MSG m, IntPtr hWnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref MSG m);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG m);

    class Entry
    {
        public double t; public int msg; public int x, y; public uint flags, mouseData, time;
        public IntPtr extra, fg;
    }

    static readonly List<Entry> log = new List<Entry>();
    static readonly Stopwatch sw = Stopwatch.StartNew();
    static HookProc keepAlive;

    static IntPtr HookProcImpl(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0)
        {
            try
            {
                var s = (MSLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(MSLLHOOKSTRUCT));
                var e = new Entry();
                e.t = sw.Elapsed.TotalMilliseconds;
                e.msg = (int)wParam;
                e.x = s.pt.X; e.y = s.pt.Y;
                e.flags = s.flags; e.mouseData = s.mouseData; e.time = s.time;
                e.extra = s.dwExtraInfo;
                e.fg = GetForegroundWindow();
                lock (log) log.Add(e);
            }
            catch { }
        }
        return CallNextHookEx(IntPtr.Zero, nCode, wParam, lParam);
    }

    static string Title(IntPtr h)
    {
        if (h == IntPtr.Zero) return "(none)";
        var sb = new StringBuilder(256);
        GetWindowText(h, sb, 256);
        string s = sb.ToString();
        return s.Length == 0 ? "(untitled:" + h.ToString("X") + ")" : s;
    }

    public static string Run(string outPath, int seconds)
    {
        keepAlive = new HookProc(HookProcImpl);
        IntPtr hhk = SetWindowsHookEx(WH_MOUSE_LL, keepAlive, GetModuleHandle(null), 0);
        if (hhk == IntPtr.Zero) return "SetWindowsHookEx failed err=" + Marshal.GetLastWin32Error();

        var start = sw.Elapsed.TotalSeconds;
        MSG m;
        while (sw.Elapsed.TotalSeconds - start < seconds)
        {
            while (PeekMessage(out m, IntPtr.Zero, 0, 0, 1)) { TranslateMessage(ref m); DispatchMessage(ref m); }
            System.Threading.Thread.Sleep(2);
        }
        while (PeekMessage(out m, IntPtr.Zero, 0, 0, 1)) { TranslateMessage(ref m); DispatchMessage(ref m); }
        UnhookWindowsHookEx(hhk);

        var sb = new StringBuilder();
        Entry[] arr;
        lock (log) arr = log.ToArray();
        foreach (var e in arr)
            sb.AppendLine(string.Format("{0:F1}\t{1}\t{2},{3}\tf={4}\tmd={5}\textra={6}\tfg={7}",
                e.t, MsgName(e.msg), e.x, e.y, e.flags, e.mouseData, e.extra.ToInt64(), Title(e.fg)));
        sb.AppendLine("=== SUMMARY ===");
        sb.AppendLine("totalEvents=" + arr.Length);
        var move = new List<Entry>();
        foreach (var e in arr) if (e.msg == WM_MOUSEMOVE) move.Add(e);
        sb.AppendLine("mouseMoveEvents=" + move.Count);
        // segment by foreground window title
        var segs = new Dictionary<string, List<Entry>>();
        foreach (var e in move)
        {
            string k = Title(e.fg);
            if (!segs.ContainsKey(k)) segs[k] = new List<Entry>();
            segs[k].Add(e);
        }
        foreach (var kv in segs)
        {
            var g = new List<double>();
            for (int i = 1; i < kv.Value.Count; i++) g.Add(kv.Value[i].t - kv.Value[i - 1].t);
            g.Sort();
            int maxJ = 0; for (int i = 1; i < kv.Value.Count; i++) { int j = Math.Abs(kv.Value[i].x - kv.Value[i - 1].x) + Math.Abs(kv.Value[i].y - kv.Value[i - 1].y); if (j > maxJ) maxJ = j; }
            sb.AppendLine(string.Format("FG[{0}] moves={1} gapMed={2} gapP90={3} gapP99={4} gapMax={5} maxStepPx={6}",
                kv.Key, kv.Value.Count,
                g.Count > 0 ? Math.Round(g[g.Count / 2], 1) : 0,
                g.Count > 0 ? Math.Round(g[(int)(g.Count * 0.9)], 1) : 0,
                g.Count > 0 ? Math.Round(g[(int)(g.Count * 0.99)], 1) : 0,
                g.Count > 0 ? Math.Round(g[g.Count - 1], 1) : 0,
                maxJ));
        }
        File.WriteAllText(outPath, sb.ToString(), Encoding.UTF8);
        return "events=" + arr.Length + " moves=" + move.Count;
    }

    static string MsgName(int m)
    {
        switch (m)
        {
            case WM_MOUSEMOVE: return "MOVE";
            case WM_LBUTTONDOWN: return "LDOWN";
            case WM_LBUTTONUP: return "LUP";
            case WM_RBUTTONDOWN: return "RDOWN";
            case WM_RBUTTONUP: return "RUP";
            case WM_MBUTTONDOWN: return "MDOWN";
            case WM_MBUTTONUP: return "MUP";
            case WM_MOUSEWHEEL: return "WHEEL";
            case WM_MOUSEHWHEEL: return "HWHEEL";
            default: return "M" + m;
        }
    }
}
