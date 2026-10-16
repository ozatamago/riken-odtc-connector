# Record several deck-check cameras (LUCID PHX064S-C) at the same time from ONE process and save JPEG frames per
# camera. Nothing moves. Location on the Fluent PC: C:\Users\Tecan\OFML\tools\camera_videos.ps1
#
#   camera_videos.ps1 -Cameras "Left=242701174,Center=234500007,Right=242701126" -Seconds 60 -OutDir <dir>
#
# STATUS 2026-10-16: does not work yet. With 3 fps and a 31.25 MB/s cap per camera, Center got all frames, Left a few
# and Right none (also with packet resend on). The cause is not known; for now record one camera with
# camera_video.ps1 and take still images of all three with camera_shot.ps1.
#
# Writes <OutDir>\<name>\frame_0001.jpg ... and frames.txt (PC time and camera timestamp in ns per frame).
#
# Why one process and lower rates (2026-10-16):
# - One camera at full rate (3072 x 2048 BayerRG8, 16.7 fps) uses about 840 Mbit/s and all three cameras reach the PC
#   through the same 1 Gbit/s link: any two at full rate lost every frame.
# - With the rate limited but one process per camera, only the camera opened last got complete frames.
# So this script opens all cameras in one process and, while recording, lowers each camera's frame rate
# (AcquisitionFrameRate) and caps its sending speed (DeviceLinkThroughputLimit, bytes per second). The previous values,
# including GevSCPD (the camera widens the packet delay itself when the cap is set and keeps it after the cap is
# switched off), are put back at the end, also after an error. Packet resend is switched on for this process (host
# side; off by default).
param(
  [string]$Cameras = 'Left=242701174,Center=234500007,Right=242701126',
  [double]$Seconds = 10,
  [string]$OutDir = 'C:\Users\Tecan\OFML\work\videos',
  [int]$Width = 1280,
  [double]$Fps = 3,
  [long]$LinkLimit = 31250000
)
$dir = 'C:\Program Files (x86)\Common Files\Tecan\Core\Recent'
$env:PATH = $dir + ';' + $env:PATH
Add-Type -AssemblyName System.Drawing
$asm = [System.Reflection.Assembly]::LoadFrom("$dir\ArenaNET_v140.dll")
$arena = $asm.GetType('ArenaNET.Arena'); $factory = $asm.GetType('ArenaNET.ImageFactory')
$fmt = $asm.GetType('ArenaNET.EPfncFormat'); $iimg = $asm.GetType('ArenaNET.IImage')
$convert = $factory.GetMethod('Convert', [Type[]]@($iimg, $fmt))
$destroy = $factory.GetMethod('Destroy', [Type[]]@($iimg)); $bgr8 = [Enum]::Parse($fmt, 'BGR8')
$jpeg = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$q = New-Object System.Drawing.Imaging.EncoderParameters 1
$q.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, [long]80)

$sys = $arena.GetMethod('OpenSystem').Invoke($null, @())
$cams = New-Object System.Collections.ArrayList
try {
  $sys.UpdateDevices(2000) | Out-Null
  foreach ($pair in $Cameras.Split(',')) {
    $name, $serial = $pair.Split('=')
    $info = $sys.Devices | Where-Object { $_.SerialNumber -eq $serial } | Select-Object -First 1
    if (-not $info) { throw "camera $name ($serial) not found" }
    $cam = @{ name = $name; dev = $sys.CreateDevice($info); out = (Join-Path $OutDir $name); lines = (New-Object System.Collections.ArrayList)
              incomplete = 0; saved = $null; streaming = $false }
    [void]$cams.Add($cam)
    New-Item -ItemType Directory -Force $cam.out | Out-Null
    $m = $cam.dev.NodeMap
    $cam.saved = @{ en = $m.GetNode('AcquisitionFrameRateEnable').Value; rate = $m.GetNode('AcquisitionFrameRate').Value
                    mode = $m.GetNode('DeviceLinkThroughputLimitMode').ToString(); lim = $m.GetNode('DeviceLinkThroughputLimit').Value
                    scpd = $m.GetNode('GevSCPD').Value }
    $m.GetNode('DeviceLinkThroughputLimitMode').FromString('On'); $m.GetNode('DeviceLinkThroughputLimit').Value = [long]$LinkLimit
    $m.GetNode('AcquisitionFrameRateEnable').Value = $true; $m.GetNode('AcquisitionFrameRate').Value = [double]$Fps
    "$name : " + [math]::Round($m.GetNode('AcquisitionFrameRate').Value, 2) + " fps, cap " + $m.GetNode('DeviceLinkThroughputLimit').Value + " B/s, exposure " + $m.GetNode('ExposureTime').ToString() + " us"
  }
  foreach ($cam in $cams) {
    # Host side only (this process), not stored in the camera: ask the camera to resend lost packets
    $cam.dev.TLStreamNodeMap.GetNode('StreamPacketResendEnable').Value = $true
    $cam.dev.StartStream(); $cam.streaming = $true
  }
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  "started " + (Get-Date -Format 'HH:mm:ss.fff')
  while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    foreach ($cam in $cams) {
      $img = $cam.dev.GetImage(3000)
      if ($img.IsIncomplete) { $cam.incomplete++ }
      else {
        $c = $convert.Invoke($null, @($img, $bgr8)); $b = $c.Bitmap; $h = [int]($b.Height * $Width / $b.Width)
        $s = New-Object System.Drawing.Bitmap $b, $Width, $h
        $s.Save((Join-Path $cam.out ('frame_{0:D4}.jpg' -f ($cam.lines.Count + 1))), $jpeg, $q); $s.Dispose()
        [void]$cam.lines.Add((Get-Date -Format 'HH:mm:ss.fff') + ' ' + $img.TimestampNs); $destroy.Invoke($null, @($c))
      }
      $cam.dev.RequeueBuffer($img)
    }
  }
  "stopped " + (Get-Date -Format 'HH:mm:ss.fff')
  foreach ($cam in $cams) {
    [System.IO.File]::WriteAllLines((Join-Path $cam.out 'frames.txt'), [string[]]$cam.lines)
    $cam.name + ": saved " + $cam.lines.Count + " frames in " + [math]::Round($sw.Elapsed.TotalSeconds, 2) + " s (incomplete " + $cam.incomplete + ")"
  }
} catch {
  "error: " + $_.Exception.Message; if ($_.Exception.InnerException) { "  inner: " + $_.Exception.InnerException.Message }
} finally {
  foreach ($cam in $cams) {
    if ($cam.streaming) { try { $cam.dev.StopStream() } catch {} }
    if ($cam.saved) {
      # Order matters: the highest allowed frame rate depends on the link cap
      try {
        $m = $cam.dev.NodeMap; $v = $cam.saved
        $m.GetNode('DeviceLinkThroughputLimit').Value = [long]$v.lim; $m.GetNode('DeviceLinkThroughputLimitMode').FromString($v.mode)
        $m.GetNode('GevSCPD').Value = [long]$v.scpd
        $m.GetNode('AcquisitionFrameRateEnable').Value = $true; $m.GetNode('AcquisitionFrameRate').Value = [double]$v.rate
        $m.GetNode('AcquisitionFrameRateEnable').Value = [bool]$v.en
        $cam.name + ": settings restored (rate " + [math]::Round($m.GetNode('AcquisitionFrameRate').Value, 2) + ", enable " + $m.GetNode('AcquisitionFrameRateEnable').Value + ", cap " + $m.GetNode('DeviceLinkThroughputLimitMode').ToString() + " " + $m.GetNode('DeviceLinkThroughputLimit').Value + ", SCPD " + $m.GetNode('GevSCPD').Value + ")"
      } catch { "error: could not restore the settings of " + $cam.name + ": " + $_.Exception.Message }
    }
    try { $sys.DestroyDevice($cam.dev) } catch {}
  }
  $arena.GetMethod('CloseSystem').Invoke($null, @($sys))
  "cameras released"
}
