# Start the "Tecan Fluent 780" SiLA server on the Fluent PC itself (ssh_target "local": no SSH needed).
# Location on the Fluent PC: C:\Users\Tecan\OFML\connector\windows\start_connector.ps1
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Tecan\OFML\connector\windows\start_connector.ps1
#
# The repository is cloned to C:\Users\Tecan\OFML\connector and its environment is made with "uv sync". The
# configuration is config.fluent780.json next to it (from config.fluent780.fluentpc.example.json; not in git).
# The output goes to C:\Users\Tecan\OFML\connector_state\server_<time>.log. Only one server with this UUID may run:
# stop the server on any other computer first.
param(
  [string]$Root = 'C:\Users\Tecan\OFML\connector',
  [string]$Config = 'C:\Users\Tecan\OFML\connector\config.fluent780.json',
  [string]$LogDir = 'C:\Users\Tecan\OFML\connector_state'
)
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $uv)) { $uv = 'uv' }
New-Item -ItemType Directory -Force $LogDir | Out-Null
$log = Join-Path $LogDir ('server_' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
Set-Location $Root
"started $(Get-Date -Format s) by $env:USERNAME (session $((Get-Process -Id $PID).SessionId))" | Out-File -Encoding ascii $log
# cmd writes the output as it comes (UTF-8 bytes); PowerShell's own redirection writes UTF-16 and reports the
# server's log lines on stderr as errors
cmd /c "`"$uv`" run connector start --app riken_odtc_connector.fluent780:create_app -cfg `"$Config`" >> `"$log`" 2>&1"
