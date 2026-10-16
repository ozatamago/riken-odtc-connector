# Record any combination of the deck-check cameras (LUCID PHX064S-C) at the same time, from ONE process, and save
# JPEG frames per camera. Nothing moves. Location on the Fluent PC: C:\Users\Tecan\OFML\tools\camera_videos.ps1
#
#   camera_videos.ps1 -Cameras "Left=242701174,Center=234500007,Right=242701126" -Seconds 60 -OutDir <dir>
#
# Writes <OutDir>\<name>\frame_0001.jpg ... and frames.txt (PC time and camera timestamp in ns per frame). Each round
# takes one frame from every camera, so the cameras' frames of one round are close in time.
#
# Why software triggers (2026-10-16): all three cameras reach the PC through one 1 Gbit/s link (port "Ethernet 2").
# One camera streaming freely is fine (16.7 fps), but with two or more streaming at once nearly every frame arrived
# incomplete, also with lower frame rates and a capped link speed. Here every camera waits for a software trigger and
# the PC triggers them one after another, waiting for each frame before triggering the next camera, so only one
# camera sends at a time. -Fps limits the rounds per second (0: as fast as possible).
#
# While recording, the trigger settings (TriggerMode On, TriggerSource Software for FrameStart) are changed; the
# previous values are put back at the end, also after an error, and printed.
param(
  [string]$Cameras = 'Left=242701174,Center=234500007,Right=242701126',
  [double]$Seconds = 10,
  [string]$OutDir = 'C:\Users\Tecan\OFML\work\videos',
  [int]$Width = 1280,
  [double]$Fps = 0
)
$dir = 'C:\Program Files (x86)\Common Files\Tecan\Core\Recent'
$env:PATH = $dir + ';' + $env:PATH
Add-Type -AssemblyName System.Drawing
$asm = [System.Reflection.Assembly]::LoadFrom("$dir\ArenaNET_v140.dll")
$arena = $asm.GetType('ArenaNET.Arena'); $factory = $asm.GetType('ArenaNET.ImageFactory')
$fmt = $asm.GetType('ArenaNET.EPfncFormat'); $iimg = $asm.GetType('ArenaNET.IImage')
$copy = $factory.GetMethod('Copy', [Type[]]@($iimg)); $convert = $factory.GetMethod('Convert', [Type[]]@($iimg, $fmt))
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
              incomplete = 0; timeouts = 0; saved = $null; streaming = $false }
    [void]$cams.Add($cam)
    New-Item -ItemType Directory -Force $cam.out | Out-Null
    $m = $cam.dev.NodeMap
    $selector = $m.GetNode('TriggerSelector').ToString()
    $m.GetNode('TriggerSelector').FromString('FrameStart')
    $cam.saved = @{ selector = $selector; mode = $m.GetNode('TriggerMode').ToString(); source = $m.GetNode('TriggerSource').ToString() }
    $m.GetNode('TriggerSource').FromString('Software'); $m.GetNode('TriggerMode').FromString('On')
    "$name : trigger " + $m.GetNode('TriggerMode').ToString() + "/" + $m.GetNode('TriggerSource').ToString() + " (was selector " + $selector + ", mode " + $cam.saved.mode + ", source " + $cam.saved.source + "), exposure " + $m.GetNode('ExposureTime').ToString() + " us"
  }
  foreach ($cam in $cams) { $cam.dev.StartStream(); $cam.streaming = $true }
  $sw = [System.Diagnostics.Stopwatch]::StartNew(); $rounds = 0
  "started " + (Get-Date -Format 'HH:mm:ss.fff')
  while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    $roundStart = $sw.Elapsed.TotalSeconds
    # Trigger and receive every camera first (only copying the image), convert afterwards: keeps the frames of one round
    # close in time
    $got = @()
    foreach ($cam in $cams) {
      $m = $cam.dev.NodeMap
      $armed = [System.Diagnostics.Stopwatch]::StartNew()
      while (-not $m.GetNode('TriggerArmed').Value -and $armed.ElapsedMilliseconds -lt 2000) { Start-Sleep -Milliseconds 2 }
      $m.GetNode('TriggerSoftware').Execute()
      $stamp = Get-Date -Format 'HH:mm:ss.fff'
      try { $img = $cam.dev.GetImage(3000) } catch { $cam.timeouts++; continue }
      if ($img.IsIncomplete) { $cam.incomplete++ } else { $got += ,@($cam, $copy.Invoke($null, @($img)), $stamp, $img.TimestampNs) }
      $cam.dev.RequeueBuffer($img)
    }
    foreach ($g in $got) {
      $cam = $g[0]
      $c = $convert.Invoke($null, @($g[1], $bgr8)); $b = $c.Bitmap; $h = [int]($b.Height * $Width / $b.Width)
      $s = New-Object System.Drawing.Bitmap $b, $Width, $h
      $s.Save((Join-Path $cam.out ('frame_{0:D4}.jpg' -f ($cam.lines.Count + 1))), $jpeg, $q); $s.Dispose()
      [void]$cam.lines.Add($g[2] + ' ' + $g[3]); $destroy.Invoke($null, @($c)); $destroy.Invoke($null, @($g[1]))
    }
    $rounds++
    if ($Fps -gt 0) { $wait = 1 / $Fps - ($sw.Elapsed.TotalSeconds - $roundStart); if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]($wait * 1000)) } }
  }
  "stopped " + (Get-Date -Format 'HH:mm:ss.fff') + ", " + $rounds + " rounds (" + [math]::Round($rounds / $sw.Elapsed.TotalSeconds, 2) + " per s)"
  foreach ($cam in $cams) {
    [System.IO.File]::WriteAllLines((Join-Path $cam.out 'frames.txt'), [string[]]$cam.lines)
    $cam.name + ": saved " + $cam.lines.Count + " frames (incomplete " + $cam.incomplete + ", timeouts " + $cam.timeouts + ")"
  }
} catch {
  "error: " + $_.Exception.Message; if ($_.Exception.InnerException) { "  inner: " + $_.Exception.InnerException.Message }
} finally {
  foreach ($cam in $cams) {
    if ($cam.streaming) { try { $cam.dev.StopStream() } catch {} }
    if ($cam.saved) {
      try {
        $m = $cam.dev.NodeMap; $v = $cam.saved
        $m.GetNode('TriggerSelector').FromString('FrameStart')
        $m.GetNode('TriggerMode').FromString($v.mode); $m.GetNode('TriggerSource').FromString($v.source)
        $cam.name + ": trigger restored (FrameStart: mode " + $m.GetNode('TriggerMode').ToString() + ", source " + $m.GetNode('TriggerSource').ToString() + "; selector " + $v.selector + ")"
        $m.GetNode('TriggerSelector').FromString($v.selector)
      } catch { "error: could not restore the trigger settings of " + $cam.name + ": " + $_.Exception.Message }
    }
    try { $sys.DestroyDevice($cam.dev) } catch {}
  }
  $arena.GetMethod('CloseSystem').Invoke($null, @($sys))
  "cameras released"
}
