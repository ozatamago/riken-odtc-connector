# OFML: register the parallel channel test task OFML_ParallelTest (same form as OFML_Screenshot)
#   Runs in Session 1 (the logged-on Tecan user) / no password stored / no trigger (runs only via schtasks /run over SSH)
$a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument 'C:\Users\Tecan\OFML\tools\run_parallel_test.vbs'
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'OFML_ParallelTest' -Action $a -Principal $p -Settings $s | Select-Object TaskName, State
