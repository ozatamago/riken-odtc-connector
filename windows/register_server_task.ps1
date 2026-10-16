# Register the Windows scheduled task OFML_Fluent780Server that runs the "Tecan Fluent 780" SiLA server on the
# Fluent PC (windows/start_connector.ps1). Same form as OFML_StartSila: runs as the logged-on user in session 1, no
# password stored. Starts when that user logs on, and on demand:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Tecan\OFML\connector\windows\register_server_task.ps1
#   schtasks /run /tn OFML_Fluent780Server        (start now)
#   schtasks /end /tn OFML_Fluent780Server        (stop)
#
# A process started from an SSH session ends with that session (2026-10-16), so the server is started this way.
# The port (50060) is not opened in the firewall: other computers reach it through an SSH tunnel.
param([string]$Root = 'C:\Users\Tecan\OFML\connector')
$name = 'OFML_Fluent780Server'
$script = Join-Path $Root 'windows\start_connector.ps1'
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $Root
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
       -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $name -Action $a -Principal $p -Trigger $t -Settings $s -Force | Select-Object TaskName, State
