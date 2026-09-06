' run_hidden.vbs - launch a PowerShell script with NO console window at all.
'
' Why (2026-09-06, Zeke): a scheduled task whose action is `powershell.exe -WindowStyle Hidden -File x.ps1`
' still CREATES a console window for a split second before PowerShell hides it. Every 10 minutes that
' flash stole focus from full-screen Rainbow Six and dropped the game out of fullscreen. WScript.Shell.Run
' with window style 0 never shows a window, so the task action becomes:
'     wscript.exe //B //NoLogo "D:\Wren-Companion\scripts\run_hidden.vbs" "D:\Wren-Companion\scripts\<script>.ps1" [args...]
' The first argument is the .ps1 path; any further arguments are passed through to the script.
Option Explicit
Dim sh, i, cmd
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count < 1 Then
    WScript.Quit 2
End If
cmd = "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & WScript.Arguments(0) & Chr(34)
For i = 1 To WScript.Arguments.Count - 1
    cmd = cmd & " " & Chr(34) & WScript.Arguments(i) & Chr(34)
Next
' 0 = hidden window, False = do not wait (the task's own time limit governs the child)
sh.Run cmd, 0, False
