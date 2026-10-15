# OFML: 並行チャネル試験のタスク OFML_ParallelTest を登録する（OFML_Screenshot と同じ形）
#   Session 1（ログオン中の Tecan ユーザー）で動く / パスワードは保存しない / トリガーなし（SSH から schtasks /run したときだけ動く）
$a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument 'C:\Users\Tecan\OFML_screen\run_parallel_test.vbs'
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'OFML_ParallelTest' -Action $a -Principal $p -Settings $s | Select-Object TaskName, State
