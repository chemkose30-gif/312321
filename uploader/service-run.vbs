' Starts run-loop.bat with no visible window (used by the scheduled task).
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = folder
sh.Run """" & folder & "\run-loop.bat""", 0, False
