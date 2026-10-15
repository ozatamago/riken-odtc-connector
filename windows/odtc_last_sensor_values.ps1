# Find the result of the ODTC's ReadActualTemperature (the SensorValues in the ResponseEvent) in the FluentControl log and print it.
# Read-only: does nothing to FluentControl or the device. Location: C:\Users\Tecan\OFML_screen\odtc_last_sensor_values.ps1
# If not found in the current log (*.ulf), also searches the previous log compressed at rollover (*.zlf, a zip file).
param([string]$Since = "")
$dir = 'C:\ProgramData\Tecan\LoggingServer\LogFiles'
$pattern = 'ResponseEvent for request:\d+ : message=Asynchronous command has finished\.[^"]*?SensorValues'
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Find-Last([string]$text) {
  $ms = [regex]::Matches($text, '<Log MsgID="\d+" TimeStamp="([^"]+)"[^>]*Message="ResponseEvent for request:[^"]*?SensorValues[^"]*"')
  if ($ms.Count -gt 0) { return $ms[$ms.Count - 1] } else { return $null }
}

$found = $null
$ulf = Get-ChildItem "$dir\*.ulf" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($ulf) {
  $fs = [System.IO.File]::Open($ulf.FullName, 'Open', 'Read', 'ReadWrite')
  $text = (New-Object System.IO.StreamReader($fs)).ReadToEnd(); $fs.Close()
  $found = Find-Last $text
}
if (-not $found) {
  $zlf = Get-ChildItem "$dir\*.zlf" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($zlf) {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($zlf.FullName)
    $text = (New-Object System.IO.StreamReader($zip.Entries[0].Open())).ReadToEnd(); $zip.Dispose()
    $found = Find-Last $text
  }
}
if ($found) {
  $ts = $found.Groups[1].Value
  if ($Since -and ($ts -lt $Since)) { "STALE $ts" } else { "LOGTIME $ts"; $found.Value }
} else { "NOTFOUND" }
