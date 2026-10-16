# Grab frames from one deck-check camera (LUCID PHX064S-C) for a few seconds and save them as JPEG files.
# Camera settings are left as they are (full resolution, auto exposure); nothing moves. Location on the Fluent PC:
# C:\Users\Tecan\OFML\tools\camera_video.ps1
#
#   camera_video.ps1 -Serial 234500007 -Seconds 5 -OutDir C:\Users\Tecan\OFML\screen\video_test
#
# Default: frames are first copied into memory while streaming (so saving does not slow the stream), then converted
# and written as frame_0001.jpg ... with frames.txt listing each frame's camera timestamp in ns. This needs about
# 100 MB of memory per second. -Live converts and writes each frame while streaming instead (any length, fewer
# frames per second; frames the camera delivers meanwhile are dropped).
param(
  [string]$Serial = '234500007',
  [double]$Seconds = 5,
  [string]$OutDir = 'C:\Users\Tecan\OFML\screen\video',
  [int]$Width = 1280,
  [switch]$Live
)
$dir = 'C:\Program Files (x86)\Common Files\Tecan\Core\Recent'
$env:PATH = $dir + ';' + $env:PATH
Add-Type -AssemblyName System.Drawing
$asm = [System.Reflection.Assembly]::LoadFrom("$dir\ArenaNET_v140.dll")
$arena = $asm.GetType('ArenaNET.Arena'); $factory = $asm.GetType('ArenaNET.ImageFactory')
$fmt = $asm.GetType('ArenaNET.EPfncFormat'); $iimg = $asm.GetType('ArenaNET.IImage')
$copy = $factory.GetMethod('Copy', [Type[]]@($iimg)); $convert = $factory.GetMethod('Convert', [Type[]]@($iimg, $fmt))
$destroy = $factory.GetMethod('Destroy', [Type[]]@($iimg)); $bgr8 = [Enum]::Parse($fmt, 'BGR8')
New-Item -ItemType Directory -Force $OutDir | Out-Null
$jpeg = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$q = New-Object System.Drawing.Imaging.EncoderParameters 1
$q.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, [long]80)

$sys = $arena.GetMethod('OpenSystem').Invoke($null, @()); $dev = $null; $streaming = $false
$frames = New-Object System.Collections.ArrayList
try {
  $sys.UpdateDevices(2000) | Out-Null
  $info = $sys.Devices | Where-Object { $_.SerialNumber -eq $Serial } | Select-Object -First 1
  if (-not $info) { throw "camera $Serial not found" }
  $dev = $sys.CreateDevice($info)
  try { "exposure " + $dev.NodeMap.GetNode('ExposureTime').ToString() + " us, auto " + $dev.NodeMap.GetNode('ExposureAuto').ToString() } catch {}
  $dev.StartStream(); $streaming = $true
  $sw = [System.Diagnostics.Stopwatch]::StartNew(); $incomplete = 0; $saved = New-Object System.Collections.ArrayList
  "started " + (Get-Date -Format 'HH:mm:ss.fff')
  while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    $img = $dev.GetImage(3000)
    if ($img.IsIncomplete) { $incomplete++ }
    elseif ($Live) {
      $c = $convert.Invoke($null, @($img, $bgr8)); $b = $c.Bitmap; $h = [int]($b.Height * $Width / $b.Width)
      $s = New-Object System.Drawing.Bitmap $b, $Width, $h
      $s.Save((Join-Path $OutDir ('frame_{0:D4}.jpg' -f ($saved.Count + 1))), $jpeg, $q); $s.Dispose()
      [void]$saved.Add((Get-Date -Format 'HH:mm:ss.fff') + ' ' + $img.TimestampNs); $destroy.Invoke($null, @($c))
    }
    else { [void]$frames.Add($copy.Invoke($null, @($img))) }
    $dev.RequeueBuffer($img)
  }
  "stopped " + (Get-Date -Format 'HH:mm:ss.fff')
  if ($Live) {
    $sw.Stop(); $dev.StopStream(); $streaming = $false
    [System.IO.File]::WriteAllLines((Join-Path $OutDir 'frames.txt'), [string[]]$saved)
    "live: saved " + $saved.Count + " frames in " + [math]::Round($sw.Elapsed.TotalSeconds, 2) + " s (incomplete " + $incomplete + ") to " + $OutDir
    return
  }
  $sw.Stop()
  $dev.StopStream(); $streaming = $false
  "grabbed " + $frames.Count + " frames in " + [math]::Round($sw.Elapsed.TotalSeconds, 2) + " s (incomplete " + $incomplete + ")"
  $lines = New-Object System.Collections.ArrayList
  for ($i = 0; $i -lt $frames.Count; $i++) {
    $f = $frames[$i]
    $c = $convert.Invoke($null, @($f, $bgr8))
    $b = $c.Bitmap; $h = [int]($b.Height * $Width / $b.Width)
    $s = New-Object System.Drawing.Bitmap $b, $Width, $h
    $s.Save((Join-Path $OutDir ('frame_{0:D4}.jpg' -f ($i + 1))), $jpeg, $q); $s.Dispose()
    [void]$lines.Add($f.TimestampNs.ToString())
    $destroy.Invoke($null, @($c)); $destroy.Invoke($null, @($f))
  }
  $frames.Clear()
  [System.IO.File]::WriteAllLines((Join-Path $OutDir 'frames.txt'), [string[]]$lines)
  "saved " + $lines.Count + " frames to " + $OutDir
} catch {
  "error: " + $_.Exception.Message; if ($_.Exception.InnerException) { "  inner: " + $_.Exception.InnerException.Message }
} finally {
  foreach ($f in $frames) { try { $destroy.Invoke($null, @($f)) } catch {} }
  if ($streaming) { try { $dev.StopStream() } catch {} }
  if ($dev) { try { $sys.DestroyDevice($dev) } catch {} }
  $arena.GetMethod('CloseSystem').Invoke($null, @($sys))
  "camera released"
}
