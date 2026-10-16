# Deck camera recorder: records any combination of the deck-check cameras (LUCID PHX064S-C) at the same time, from
# ONE process, until asked to stop, and takes full-size still images on request while recording. Nothing moves.
# Location on the Fluent PC: C:\Users\Tecan\OFML\tools\camera_videos.ps1
#
#   camera_videos.ps1 -Cameras "Left=242701174,Center=234500007,Right=242701126" -Record "Left,Right" -OutDir <dir>
#
# - Writes <OutDir>\<name>\frame_0001.jpg ... and frames.txt (PC time and camera timestamp in ns) for each camera in
#   -Record. Each round takes one frame from each camera, so the frames of one round are close in time.
# - Stops when the file <OutDir>\stop appears, or after -MaxSeconds.
# - Still images while recording: a file <OutDir>\requests\<id>.req with one line "<names>|<folder>|<tag>" (names
#   separated by commas; any of -Cameras, recorded or not) is answered in the next round with full-size PNGs plus
#   JPEG previews in <folder>, and <id>.done holding the same lines as camera_shot.ps1
#   ("OK <name> <png> <preview> <width>x<height>" or "ERROR <name> <message>").
# - While it runs, -StateFile holds {pid, out_dir, record, cameras, started}; it is removed at the end. Other programs
#   must not open the cameras while it exists (ask the recorder for still images instead).
#
# Why software triggers (2026-10-16): all three cameras reach the PC through one 1 Gbit/s link (port "Ethernet 2").
# One camera streaming freely is fine (16.7 fps), but with two or more streaming at once nearly every frame arrived
# incomplete, also with lower frame rates and a capped link speed. Here every camera waits for a software trigger and
# the PC triggers them one after another, so only one camera sends at a time: three cameras gave 2.1 rounds per second
# with the frames of a round within 0.22 s, two cameras 3.2 rounds per second. -Fps limits the rounds per second.
#
# The trigger settings (TriggerMode On, TriggerSource Software for FrameStart) are put back at the end, also after an
# error, and printed. The process keeps running if the SSH connection that started it is closed.
param(
  [string]$Cameras = 'Left=242701174,Center=234500007,Right=242701126',
  [string]$Record = 'Left,Center,Right',
  [string]$OutDir = 'C:\Users\Tecan\OFML\work\videos',
  [int]$Width = 1280,
  [double]$Fps = 0,
  [double]$MaxSeconds = 1800,
  [string]$StateFile = 'C:\Users\Tecan\OFML\state\recorder.json',
  [int]$PreviewWidth = 1280
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

function Save-Scaled($bmp, $path, $width) {
  $h = [int]($bmp.Height * $width / $bmp.Width)
  $s = New-Object System.Drawing.Bitmap $bmp, $width, $h
  $s.Save($path, $jpeg, $q); $s.Dispose()
}

$recordNames = @($Record.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$reqDir = Join-Path $OutDir 'requests'
New-Item -ItemType Directory -Force $reqDir | Out-Null
New-Item -ItemType Directory -Force (Split-Path $StateFile) | Out-Null
$sys = $arena.GetMethod('OpenSystem').Invoke($null, @())
$cams = [ordered]@{}
try {
  if (Test-Path $StateFile) {
    $old = Get-Content $StateFile -Raw | ConvertFrom-Json
    if (Get-Process -Id $old.pid -ErrorAction SilentlyContinue) { throw "another recorder is running (pid $($old.pid), $($old.out_dir))" }
  }
  $sys.UpdateDevices(2000) | Out-Null
  foreach ($pair in $Cameras.Split(',')) {
    $name, $serial = $pair.Split('=')
    $info = $sys.Devices | Where-Object { $_.SerialNumber -eq $serial } | Select-Object -First 1
    if (-not $info) { throw "camera $name ($serial) not found" }
    $cam = @{ name = $name; dev = $sys.CreateDevice($info); out = (Join-Path $OutDir $name); lines = (New-Object System.Collections.ArrayList)
              incomplete = 0; timeouts = 0; saved = $null; streaming = $false; record = ($recordNames -contains $name) }
    $cams[$name] = $cam
    $m = $cam.dev.NodeMap
    $selector = $m.GetNode('TriggerSelector').ToString()
    $m.GetNode('TriggerSelector').FromString('FrameStart')
    $cam.saved = @{ selector = $selector; mode = $m.GetNode('TriggerMode').ToString(); source = $m.GetNode('TriggerSource').ToString() }
    $m.GetNode('TriggerSource').FromString('Software'); $m.GetNode('TriggerMode').FromString('On')
    if ($cam.record) { New-Item -ItemType Directory -Force $cam.out | Out-Null }
    "$name : trigger On/Software (was mode " + $cam.saved.mode + ", source " + $cam.saved.source + ")" + $(if ($cam.record) { ', recording' } else { '' })
  }
  $unknown = @($recordNames | Where-Object { -not $cams.Contains($_) })
  if ($unknown.Count) { throw "unknown camera(s) to record: $($unknown -join ', ')" }
  foreach ($cam in $cams.Values) { $cam.dev.StartStream(); $cam.streaming = $true }
  @{ pid = $PID; out_dir = $OutDir; record = $recordNames; cameras = @($cams.Keys); started = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') } |
    ConvertTo-Json | Set-Content -Encoding UTF8 $StateFile
  $sw = [System.Diagnostics.Stopwatch]::StartNew(); $rounds = 0
  "started " + (Get-Date -Format 'HH:mm:ss.fff')
  while ($sw.Elapsed.TotalSeconds -lt $MaxSeconds -and -not (Test-Path (Join-Path $OutDir 'stop'))) {
    $roundStart = $sw.Elapsed.TotalSeconds
    # Still-image requests: { file, names, folder, tag }
    $requests = @()
    foreach ($f in @(Get-ChildItem $reqDir -Filter *.req -ErrorAction SilentlyContinue)) {
      $parts = (Get-Content $f.FullName -Raw).Trim().Split('|')
      $requests += ,@{ file = $f.FullName; names = @($parts[0].Split(',') | ForEach-Object { $_.Trim() }); folder = $parts[1]; tag = $(if ($parts.Count -gt 2) { $parts[2] } else { '' }) }
    }
    $wanted = @($cams.Keys | Where-Object { $n = $_; $cams[$n].record -or @($requests | Where-Object { $_.names -contains $n }).Count })
    # Trigger and receive every wanted camera first (only copying the image), convert afterwards: keeps the frames of
    # one round close in time
    $got = @{}
    foreach ($n in $wanted) {
      $cam = $cams[$n]; $m = $cam.dev.NodeMap
      $armed = [System.Diagnostics.Stopwatch]::StartNew()
      while (-not $m.GetNode('TriggerArmed').Value -and $armed.ElapsedMilliseconds -lt 2000) { Start-Sleep -Milliseconds 2 }
      $m.GetNode('TriggerSoftware').Execute()
      $stamp = Get-Date -Format 'HH:mm:ss.fff'
      try { $img = $cam.dev.GetImage(3000) } catch { $cam.timeouts++; continue }
      if ($img.IsIncomplete) { $cam.incomplete++ } else { $got[$n] = @($copy.Invoke($null, @($img)), $stamp, $img.TimestampNs) }
      $cam.dev.RequeueBuffer($img)
    }
    $answers = @{}
    foreach ($n in $got.Keys) {
      $cam = $cams[$n]; $c = $convert.Invoke($null, @($got[$n][0], $bgr8)); $b = $c.Bitmap
      if ($cam.record) {
        Save-Scaled $b (Join-Path $cam.out ('frame_{0:D4}.jpg' -f ($cam.lines.Count + 1))) $Width
        [void]$cam.lines.Add($got[$n][1] + ' ' + $got[$n][2])
      }
      foreach ($r in $requests) {
        if ($r.names -notcontains $n) { continue }
        New-Item -ItemType Directory -Force $r.folder | Out-Null
        $base = Join-Path $r.folder ((Get-Date -Format 'yyyyMMdd-HHmmss') + '_' + $n + $(if ($r.tag) { '_' + $r.tag } else { '' }))
        $b.Save($base + '.png', [System.Drawing.Imaging.ImageFormat]::Png)
        Save-Scaled $b ($base + '_preview.jpg') $PreviewWidth
        $answers["$($r.file)|$n"] = "OK $n $base.png ${base}_preview.jpg $($b.Width)x$($b.Height)"
      }
      $destroy.Invoke($null, @($c)); $destroy.Invoke($null, @($got[$n][0]))
    }
    foreach ($r in $requests) {
      $lines = foreach ($n in $r.names) {
        if (-not $cams.Contains($n)) { "ERROR $n unknown camera" }
        elseif ($answers.Contains("$($r.file)|$n")) { $answers["$($r.file)|$n"] }
        else { "ERROR $n no complete frame in this round" }
      }
      [System.IO.File]::WriteAllLines(($r.file -replace '\.req$', '.done'), [string[]]$lines)
      Remove-Item $r.file -ErrorAction SilentlyContinue
    }
    $rounds++
    if ($Fps -gt 0) { $wait = 1 / $Fps - ($sw.Elapsed.TotalSeconds - $roundStart); if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]($wait * 1000)) } }
  }
  "stopped " + (Get-Date -Format 'HH:mm:ss.fff') + ", " + $rounds + " rounds in " + [math]::Round($sw.Elapsed.TotalSeconds, 1) + " s" + $(if ($sw.Elapsed.TotalSeconds -ge $MaxSeconds) { ' (time limit)' } else { '' })
  foreach ($cam in $cams.Values) {
    if (-not $cam.record) { continue }
    [System.IO.File]::WriteAllLines((Join-Path $cam.out 'frames.txt'), [string[]]$cam.lines)
    "RECORDED " + $cam.name + " " + $cam.lines.Count + " " + $cam.incomplete + " " + $cam.timeouts + " " + $cam.out
  }
} catch {
  "error: " + $_.Exception.Message; if ($_.Exception.InnerException) { "  inner: " + $_.Exception.InnerException.Message }
} finally {
  foreach ($cam in $cams.Values) {
    if ($cam.streaming) { try { $cam.dev.StopStream() } catch {} }
    if ($cam.saved) {
      try {
        $m = $cam.dev.NodeMap; $v = $cam.saved
        $m.GetNode('TriggerSelector').FromString('FrameStart')
        $m.GetNode('TriggerMode').FromString($v.mode); $m.GetNode('TriggerSource').FromString($v.source)
        $cam.name + ": trigger restored (mode " + $m.GetNode('TriggerMode').ToString() + ", source " + $m.GetNode('TriggerSource').ToString() + ")"
        $m.GetNode('TriggerSelector').FromString($v.selector)
      } catch { "error: could not restore the trigger settings of " + $cam.name + ": " + $_.Exception.Message }
    }
    try { $sys.DestroyDevice($cam.dev) } catch {}
  }
  $arena.GetMethod('CloseSystem').Invoke($null, @($sys))
  # Unanswered requests and the state file go last, so nobody waits on a recorder that has gone
  foreach ($f in @(Get-ChildItem $reqDir -Filter *.req -ErrorAction SilentlyContinue)) {
    [System.IO.File]::WriteAllLines(($f.FullName -replace '\.req$', '.done'), [string[]]@('ERROR - the recorder stopped'))
    Remove-Item $f.FullName -ErrorAction SilentlyContinue
  }
  if (Test-Path $StateFile) {
    try { if ((Get-Content $StateFile -Raw | ConvertFrom-Json).pid -eq $PID) { Remove-Item $StateFile } } catch {}
  }
  "cameras released"
}
