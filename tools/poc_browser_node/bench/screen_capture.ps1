# A PNG of a rectangle of the screen: the headed LNA check's evidence (a
# permission bubble is browser UI, which a page screenshot cannot see).
#   powershell -NoProfile -File screen_capture.ps1 -Out shot.png -X 0 -Y 0 -W 1200 -H 800
param([string]$Out, [int]$X = 0, [int]$Y = 0, [int]$W = 1200, [int]$H = 800)
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap $W, $H
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($X, $Y, 0, 0, $bitmap.Size)
$bitmap.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
