using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// Proven instrument: high-rate cursor trajectory + system-wide last-input timer.
// Distinguishes (a) mouse alive and smooth, (b) packet loss / bursty reports,
// (c) cursor drifting on its own (phantom source), (d) zero input at all.
public class Track
{
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }

    static readonly int[] KV = { 0x10, 0x11, 0x12, 0x41, 0x42, 0x43, 0x44, 0x45, 0x20, 0x0D, 0x1B, 0x25 };
    static readonly string[] KN = { "Shift", "Ctrl", "Alt", "A", "B", "C", "D", "E", "Space", "Enter", "Esc", "PgUp" };

    public static string Run(string outPath, int seconds)
    {
        var sw = Stopwatch.StartNew();
        var sb = new StringBuilder();
        int px = -1, py = -1;
        int updates = 0, maxJump = 0, totalJump = 0;
        double lastMoveT = 0;
        double firstMoveT = -1, lastMoveWall = -1;
        var gaps = new List<double>();
        var jumps = new List<int>();
        var keyHits = new int[KV.Length];
        int total = 0;

        while (sw.Elapsed.TotalSeconds < seconds)
        {
            POINT p;
            GetCursorPos(out p);
            for (int i = 0; i < KV.Length; i++)
                if ((GetAsyncKeyState(KV[i]) & 0x8000) != 0) keyHits[i]++;

            if (px >= 0 && (p.X != px || p.Y != py))
            {
                int jump = Math.Abs(p.X - px) + Math.Abs(p.Y - py);
                double t = sw.Elapsed.TotalMilliseconds;
                if (lastMoveT > 0) gaps.Add(t - lastMoveT);
                if (firstMoveT < 0) firstMoveT = t;
                lastMoveT = t; lastMoveWall = t;
                updates++; totalJump += jump; if (jump > maxJump) maxJump = jump;
                jumps.Add(jump);
                sb.AppendLine(string.Format("{0:F1}\t{1}\t{2}\td={3}", t, p.X, p.Y, jump));
            }
            px = p.X; py = p.Y;
            total++;
            // spin-wait to ~4ms resolution (Start-Sleep on Windows has ~15ms granularity)
            double target = sw.Elapsed.TotalMilliseconds + 4.0;
            while (sw.Elapsed.TotalMilliseconds < target) { }
        }

        LASTINPUTINFO lii = new LASTINPUTINFO();
        lii.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
        uint idleMs = 0;
        if (GetLastInputInfo(ref lii)) idleMs = unchecked((uint)Environment.TickCount) - lii.dwTime;

        sb.AppendLine("=== SUMMARY ===");
        sb.AppendLine(string.Format("durationSec={0} samples={1} effectivePollHz={2}", seconds, total, Math.Round(total / (double)seconds, 1)));
        sb.AppendLine(string.Format("cursorUpdates={0} maxJumpPx={1} avgJumpPx={2}", updates, maxJump, updates > 0 ? Math.Round((double)totalJump / updates, 1) : 0));
        sb.AppendLine(string.Format("firstMoveAtMs={0} lastMoveAtMs={1} idleSinceLastInputMs={2}", firstMoveT, lastMoveWall, idleMs));
        if (gaps.Count > 0)
        {
            var sorted = new List<double>(gaps); sorted.Sort();
            sb.AppendLine(string.Format("gapMs median={0} p90={1} p99={2} max={3}",
                Math.Round(sorted[sorted.Count / 2], 1),
                Math.Round(sorted[(int)(sorted.Count * 0.9)], 1),
                Math.Round(sorted[(int)(sorted.Count * 0.99)], 1),
                Math.Round(sorted[sorted.Count - 1], 1)));
            int big = 0; foreach (var g in gaps) if (g > 100) big++;
            sb.AppendLine(string.Format("stalls>100ms={0} of {1} intervals", big, gaps.Count));
        }
        sb.AppendLine("=== KEY HITS (keyboard activity during window) ===");
        for (int i = 0; i < KV.Length; i++)
            if (keyHits[i] > 0) sb.AppendLine(string.Format("{0}={1}", KN[i], keyHits[i]));

        File.WriteAllText(outPath, sb.ToString());
        return string.Format("updates={0} maxJump={1} idleMs={2}", updates, maxJump, idleMs);
    }
}
