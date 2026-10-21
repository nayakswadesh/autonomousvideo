$ErrorActionPreference = 'Stop'
$revision = '2b75f3ad12b420c9504ef05df8d2564a28f8500e'
$root = Join-Path $PSScriptRoot '..\src\autonomous_studio\assets\semantic\phosphor'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$icons = @(
  'book-open',
  'books',
  'clipboard-text',
  'magnifying-glass',
  'sun',
  'battery-charging',
  'lightbulb',
  'clock',
  'thermometer',
  'drop',
  'recycle',
  'warning-circle'
)
foreach ($name in $icons) {
  $target = Join-Path $root "$name.svg"
  if (-not (Test-Path -LiteralPath $target)) {
    $url = "https://raw.githubusercontent.com/phosphor-icons/core/$revision/assets/regular/$name.svg"
    Invoke-WebRequest -Uri $url -OutFile $target
  }
}
$license = Join-Path $root 'LICENSE'
if (-not (Test-Path -LiteralPath $license)) {
  Invoke-WebRequest -Uri "https://raw.githubusercontent.com/phosphor-icons/core/$revision/LICENSE" -OutFile $license
}
Write-Output "Imported $($icons.Count) Phosphor icons and MIT license from $revision"
