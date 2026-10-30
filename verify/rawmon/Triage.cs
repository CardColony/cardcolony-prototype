using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// Mouse-less triage: detect stuck buttons/modifiers, self-drift of the cursor,
// and whether ANY raw mouse report arrives (raw input works without a mouse).
public class Triage
{
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }

    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")] public static extern short GetKeyState(int vKey);
    [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }

    static readonly string[] KN = { "LMB", "RMB", "MMB", "Shift", "Ctrl", "Alt", "LWin", "RWin", "CapsLock", "NumLock", "ScrollLock" };
    static readonly int[] KV = { 0x01, 0x02, 0x04, 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x14, 0x90, 0x91 };

    public static string Run(string outPath, int seconds)
    {
        var sw = Stopwatch.StartNew();
        var sb = new StringBuilder();
        var keyDown = new int[KV.Length];
        int total = 0;
        int prevX = -1, prevY = -1;
        int moves = 0, maxJump = 0;
        var posLog = new List<string>();
        double lastMoveT = -1;

        while (sw.Elapsed.TotalSeconds < seconds)
        {
            POINT p;
            GetCursorPos(out p);
            for (int i = 0; i < KV.Length; i++)
                if ((GetAsyncKeyState(KV[i]) & 0x8000) != 0) keyDown[i]++;

            if (p.X != prevX || p.Y != prevY)
            {
                int jump = (prevX < 0) ? 0 : Math.Abs(p.X - prevX) + Math.Abs(p.Y - prevY);
                if (jump > maxJump) maxJump = jump;
                moves++;
                lastMoveT = sw.Elapsed.TotalMilliseconds;
                posLog.Add(string.Format("{0:F0}\t{1}\t{2}\td={3}", sw.Elapsed.TotalMilliseconds, p.X, p.Y, jump));
            }
            prevX = p.X; prevY = p.Y;
            total++;
            System.Threading.Thread.Sleep(10);
        }

        LASTINPUTINFO lii = new LASTINPUTINFO();
        lii.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
        uint idleMs = 0;
        if (GetLastInputInfo(ref lii)) idleMs = unchecked((uint)Environment.TickCount) - lii.dwTime;

        sb.AppendLine("=== KEY STATE (samples down / total) ===");
        for (int i = 0; i < KV.Length; i++)
        {
            string verdict = keyDown[i] == 0 ? "never" : (keyDown[i] > total * 0.9 ? "*** STUCK ***" : "pressed");
            sb.AppendLine(string.Format("{0,-12} down={1,-6} of {2,-6}  {3}", KN[i], keyDown[i], total, verdict));
        }
        sb.AppendLine("=== CURSOR ===");
        sb.AppendLine(string.Format("samples={0} moves={1} maxJumpPx={2} idleSinceLastInputMs={3}", total, moves, maxJump, idleMs));
        sb.AppendLine("=== POSITION LOG ===");
        foreach (var l in posLog) sb.AppendLine(l);

        File.WriteAllText(outPath, sb.ToString());
        return string.Format("samples={0} moves={1} maxJump={2} idleMs={3}", total, moves, maxJump, idleMs);
    }
}
