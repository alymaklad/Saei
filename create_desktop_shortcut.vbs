' One-time setup: double-click this file once. It creates a real Windows
' Desktop shortcut ("Job Application Agent") that runs run.bat -- the API,
' scheduler, and dashboard all start from that one icon after this.
'
' Safe to run more than once; it just overwrites the same shortcut.

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
targetBat = scriptDir & "\run.bat"

If Not fso.FileExists(targetBat) Then
    MsgBox "Couldn't find run.bat next to this script (expected at " & targetBat & ")." & vbCrLf & _
           "Make sure create_desktop_shortcut.vbs stays in the project folder.", vbExclamation, "Job Application Agent"
    WScript.Quit 1
End If

desktop = shell.SpecialFolders("Desktop")
shortcutPath = desktop & "\Job Application Agent.lnk"

Set shortcut = shell.CreateShortcut(shortcutPath)
shortcut.TargetPath = targetBat
shortcut.WorkingDirectory = scriptDir
shortcut.WindowStyle = 1
shortcut.Description = "Run the Job Application Agent (API + Scheduler + Dashboard)"
shortcut.IconLocation = "shell32.dll,3"
shortcut.Save

MsgBox "Desktop shortcut created: " & shortcutPath & vbCrLf & vbCrLf & _
       "Double-click it any time to start the agent.", vbInformation, "Job Application Agent"
