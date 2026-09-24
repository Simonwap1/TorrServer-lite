# Скачивает portable Python в tools\python (если ещё нет).
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$PyDir = Join-Path $Root "tools\python"
$PyExe = Join-Path $PyDir "python.exe"
if (Test-Path $PyExe) {
  Write-Host "Python already: $PyExe"
  exit 0
}

$Ver = "3.12.8"
$Url = "https://www.python.org/ftp/python/$Ver/python-$Ver-embed-amd64.zip"
$Zip = Join-Path $env:TEMP "python-embed-$Ver.zip"
Write-Host "Downloading portable Python $Ver ..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
New-Item -ItemType Directory -Force -Path $PyDir | Out-Null
Expand-Archive -Path $Zip -DestinationPath $PyDir -Force
Remove-Item $Zip -Force -EA SilentlyContinue

Get-ChildItem $PyDir -Filter "python*._pth" | ForEach-Object {
  $txt = Get-Content $_.FullName -Raw
  $txt = $txt -replace "(?m)^#import site", "import site"
  if ($txt -notmatch "(?m)^import site") {
    $txt = $txt.TrimEnd() + "`r`nimport site`r`n"
  }
  Set-Content -Path $_.FullName -Value $txt -Encoding ASCII
}
if (-not (Test-Path $PyExe)) { throw "python.exe missing after extract" }
Write-Host "OK: $PyExe"
