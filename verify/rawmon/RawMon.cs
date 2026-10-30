using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

// Raw input monitor: records which HID device actually generates mouse reports
// and the effective report rate, so we can tell "which device drives the cursor"
// and whether reports are arriving in bursts (packet loss / low polling).
public class RawMon
{
    const uint WM_INPUT = 0x00FF;
    const uint RIDI_PREPARSEDDATA = 0x00000003;
    const uint RIDI_DEVICEPATH = 0x00000002;
    const uint RIM_TYPEMOUSE = 0;
    const uint RIDEV_INPUTSINK = 0x00000100;
    const uint PM_REMOVE = 0x0001;

    [StructLayout(LayoutKind.Sequential)]
    struct RAWINPUTDEVICE { public ushort usUsagePage; public ushort usUsage; public uint dwFlags; public IntPtr hwndTarget; }

    [StructLayout(LayoutKind.Sequential)]
    struct RAWINPUTHEADER { public uint dwType; public uint dwSize; public IntPtr hDevice; public IntPtr wParam; }

    [StructLayout(LayoutKind.Sequential)]
    struct MOUSEREPORT { public uint dwFlags; public int dx; public int dy; public uint mouseData; public uint dwFlags2; public uint time; }

    [StructLayout(LayoutKind.Sequential)]
    struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam; public IntPtr lParam; public uint time; public int px; public int py; public uint lPrivate; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct WNDCLASSEX
    {
        public uint cbSize; public uint style; public IntPtr lpfnWndProc; public int cbClsExtra; public int cbWndExtra;
        public IntPtr hInstance; public IntPtr hIcon; public IntPtr hCursor; public IntPtr hbrBackground;
        public IntPtr lpszMenuName; public IntPtr lpszClassName; public IntPtr hIconSm;
    }

    delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)] static extern bool RegisterRawInputDevices([In] RAWINPUTDEVICE[] p, uint n, uint size);
    [DllImport("user32.dll", SetLastError = true)] static extern uint GetRawInputData(IntPtr hRawInput, uint cmd, IntPtr pData, ref uint pcbSize, uint cbSizeHeader);
    [DllImport("user32.dll", SetLastError = true)] static extern uint GetRawInputDeviceInfo(IntPtr hDevice, uint cmd, IntPtr pInfo, ref uint pcbSize);
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern ushort RegisterClassEx(ref WNDCLASSEX lpwcx);
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern IntPtr CreateWindowEx(uint ex, string cls, string title, uint style, int x, int y, int w, int h, IntPtr parent, IntPtr menu, IntPtr inst, IntPtr param);
    [DllImport("user32.dll")] static extern IntPtr DefWindowProc(IntPtr hWnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern bool PeekMessage(out MSG msg, IntPtr hWnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref MSG msg);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG msg);
    [DllImport("kernel32.dll")] static extern IntPtr GetModuleHandle(string name);

    static WndProcDelegate keepAlive;
    static readonly Dictionary<IntPtr, string> devNames = new Dictionary<IntPtr, string>();
    static readonly List<string> lines = new List<string>();
    static readonly Stopwatch sw = Stopwatch.StartNew();

    static string DevicePath(IntPtr hDevice)
    {
        uint size = 0;
        GetRawInputDeviceInfo(hDevice, RIDI_DEVICEPATH, IntPtr.Zero, ref size);
        if (size == 0) return "(unknown)";
        IntPtr buf = Marshal.AllocHGlobal((int)size);
        try
        {
            uint n = GetRawInputDeviceInfo(hDevice, RIDI_DEVICEPATH, buf, ref size);
            if (n == unchecked((uint)-1)) return "(err)";
            return Marshal.PtrToStringUni(buf) ?? "(null)";
        }
        finally { Marshal.FreeHGlobal(buf); }
    }

    static IntPtr WndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == WM_INPUT)
        {
            try
            {
                uint size = 0;
                int hdrSize = Marshal.SizeOf(typeof(RAWINPUTHEADER));
                GetRawInputData(lParam, RIDI_PREPARSEDDATA, IntPtr.Zero, ref size, (uint)hdrSize);
                if (size > 0 && size < 4096)
                {
                    IntPtr buf = Marshal.AllocHGlobal((int)size);
                    try
                    {
                        uint got = GetRawInputData(lParam, RIDI_PREPARSEDDATA, buf, ref size, (uint)hdrSize);
                        if (got != unchecked((uint)-1))
                        {
                            uint type = (uint)Marshal.ReadInt32(buf, 0);
                            IntPtr hDevice = Marshal.ReadIntPtr(buf, 8);
                            if (type == RIM_TYPEMOUSE)
                            {
                                var rep = (MOUSEREPORT)Marshal.PtrToStructure(buf + hdrSize, typeof(MOUSEREPORT));
                                string path;
                                if (!devNames.TryGetValue(hDevice, out path)) { path = DevicePath(hDevice); devNames[hDevice] = path; }
                                lock (lines)
                                {
                                    lines.Add(string.Format("{0}\t{1}\tdx={2}\tdy={3}\tflags={4}\thDev={5}",
                                        sw.Elapsed.TotalMilliseconds.ToString("F2"), path, rep.dx, rep.dy, rep.dwFlags, hDevice.ToInt64()));
                                }
                            }
                        }
                    }
                    finally { Marshal.FreeHGlobal(buf); }
                }
            }
            catch { }
        }
        return DefWindowProc(hWnd, msg, wParam, lParam);
    }

    public static string Run(string outPath, int seconds)
    {
        keepAlive = new WndProcDelegate(WndProc);
        WNDCLASSEX wc = new WNDCLASSEX();
        wc.cbSize = (uint)Marshal.SizeOf(typeof(WNDCLASSEX));
        wc.style = 0;
        wc.lpfnWndProc = Marshal.GetFunctionPointerForDelegate(keepAlive);
        wc.hInstance = GetModuleHandle(null);
        wc.lpszClassName = Marshal.StringToHGlobalUni("RawMonCls" + Guid.NewGuid().ToString("N").Substring(0, 8));
        if (RegisterClassEx(ref wc) == 0) return "RegisterClassEx failed err=" + Marshal.GetLastWin32Error();

        IntPtr hwnd = CreateWindowEx(0, Marshal.PtrToStringUni(wc.lpszClassName), "RawMon", 0, 0, 0, 0, 0,
                                     new IntPtr(-3), IntPtr.Zero, wc.hInstance, IntPtr.Zero);
        if (hwnd == IntPtr.Zero) return "CreateWindowEx failed err=" + Marshal.GetLastWin32Error();

        RAWINPUTDEVICE[] rid = new RAWINPUTDEVICE[1];
        rid[0].usUsagePage = 0x01;   // generic desktop
        rid[0].usUsage = 0x02;       // mouse
        rid[0].dwFlags = RIDEV_INPUTSINK;
        rid[0].hwndTarget = hwnd;
        if (!RegisterRawInputDevices(rid, 1, (uint)Marshal.SizeOf(typeof(RAWINPUTDEVICE))))
            return "RegisterRawInputDevices failed err=" + Marshal.GetLastWin32Error();

        var deadline = sw.Elapsed.TotalSeconds + seconds;
        MSG m;
        while (sw.Elapsed.TotalSeconds < deadline)
        {
            while (PeekMessage(out m, IntPtr.Zero, 0, 0, PM_REMOVE)) { TranslateMessage(ref m); DispatchMessage(ref m); }
            System.Threading.Thread.Sleep(5);
        }
        while (PeekMessage(out m, IntPtr.Zero, 0, 0, PM_REMOVE)) { TranslateMessage(ref m); DispatchMessage(ref m); }

        lock (lines) File.WriteAllLines(outPath, lines);
        return "events=" + lines.Count + " distinctDevices=" + devNames.Count;
    }
}
