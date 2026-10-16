# Take one image from each given deck-check camera (LUCID PHX064S-C, GigE) and save it as PNG, with a small JPEG
# preview next to it. Does not move anything and does not use FluentControl. Location on the Fluent PC:
# C:\Users\Tecan\OFML\tools\camera_shot.ps1
#
#   camera_shot.ps1 -Cameras "Center=234500007,Left=242701174" -OutDir C:\Users\Tecan\OFML\records\... -Tag start
#
# Output: one line per camera, "OK <name> <png path> <preview path> <width>x<height>" or "ERROR <name> <message>".
# Do not run this while FluentControl's own Deck Check uses the cameras (opening a camera from two programs at once
# can disturb it).
param(
  [string]$Cameras = 'Center=234500007',
  [string]$OutDir = 'C:\Users\Tecan\OFML\screen',
  [string]$Tag = '',
  [int]$PreviewWidth = 1280
)
$dir = 'C:\Program Files (x86)\Common Files\Tecan\Core\Recent'
$env:PATH = $dir + ';' + $env:PATH
Add-Type -AssemblyName System.Drawing
$asm = [System.Reflection.Assembly]::LoadFrom("$dir\ArenaNET_v140.dll")
$arena = $asm.GetType('ArenaNET.Arena'); $factory = $asm.GetType('ArenaNET.ImageFactory')
$fmt = $asm.GetType('ArenaNET.EPfncFormat'); $iimg = $asm.GetType('ArenaNET.IImage')
New-Item -ItemType Directory -Force $OutDir | Out-Null
$jpeg = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$q = New-Object System.Drawing.Imaging.EncoderParameters 1
$q.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, [long]75)

$sys = $arena.GetMethod('OpenSystem').Invoke($null, @())
try {
  $sys.UpdateDevices(2000) | Out-Null
  foreach ($pair in $Cameras.Split(',')) {
    $name, $serial = $pair.Split('=')
    $dev = $null; $img = $null; $streaming = $false
    try {
      $info = $sys.Devices | Where-Object { $_.SerialNumber -eq $serial } | Select-Object -First 1
      if (-not $info) { throw "camera $serial not found" }
      $dev = $sys.CreateDevice($info)
      $dev.StartStream(); $streaming = $true
      $img = $dev.GetImage(5000)
      $bgr = $factory.GetMethod('Convert', [Type[]]@($iimg, $fmt)).Invoke($null, @($img, [Enum]::Parse($fmt, 'BGR8')))
      $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
      $base = Join-Path $OutDir ($stamp + '_' + $name + $(if ($Tag) { '_' + $Tag } else { '' }))
      $bmp = $bgr.Bitmap
      $bmp.Save($base + '.png', [System.Drawing.Imaging.ImageFormat]::Png)
      $h = [int]($bmp.Height * $PreviewWidth / $bmp.Width)
      $small = New-Object System.Drawing.Bitmap $bmp, $PreviewWidth, $h
      $small.Save($base + '_preview.jpg', $jpeg, $q); $small.Dispose()
      "OK " + $name + " " + $base + ".png " + $base + "_preview.jpg " + $img.Width + "x" + $img.Height
      $factory.GetMethod('Destroy', [Type[]]@($iimg)).Invoke($null, @($bgr))
    } catch {
      $m = $_.Exception.Message; if ($_.Exception.InnerException) { $m += ' / ' + $_.Exception.InnerException.Message }
      "ERROR " + $name + " " + $m
    } finally {
      if ($img -and $dev) { try { $dev.RequeueBuffer($img) } catch {} }
      if ($streaming) { try { $dev.StopStream() } catch {} }
      if ($dev) { try { $sys.DestroyDevice($dev) } catch {} }
    }
  }
} finally {
  $arena.GetMethod('CloseSystem').Invoke($null, @($sys))
}
