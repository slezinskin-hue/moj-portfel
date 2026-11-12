<#
Buduje instalator "Mój Portfel" (dist\MojPortfel-Setup-<wersja>.exe).
Kroki: Python embeddable (python.org, suma SHA-256 sprawdzana) -> biblioteki z requirements.txt (tylko gotowe paczki .whl dla Windows x64)
-> kod aplikacji + plik _build.py (repozytorium GitHub do sprawdzania aktualizacji) -> kompilacja .pyc -> Inno Setup.
Uzycie lokalne:   powershell -ExecutionPolicy Bypass -File instalator\build.ps1 -UpdateRepo "uzytkownik/moj-portfel"
W GitHub Actions: repozytorium bierze sie z GITHUB_REPOSITORY, wersja jest sprawdzana z tagiem (GITHUB_REF_NAME).
Niczego nie instaluje w systemie: wszystko powstaje w build\ (pliki tymczasowe) i dist\ (instalator).
#>
param(
    [string]$PythonVersion = "3.12.10",
    [string]$PythonSha256 = "",
    [string]$HostPython = "python",
    [string]$Iscc = "",
    [string]$UpdateRepo = $env:GITHUB_REPOSITORY
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Version = (Select-String -Path "$Root\invapp\__init__.py" -Pattern '__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"').Matches[0].Groups[1].Value
if ($env:GITHUB_REF_TYPE -eq "tag" -and $env:GITHUB_REF_NAME -ne "v$Version") { throw "Tag $($env:GITHUB_REF_NAME) nie zgadza sie z wersja w invapp\__init__.py ($Version)." }
$Build = Join-Path $Root "build"; $Cache = Join-Path $Build "cache"; $Stage = Join-Path $Build "stage"; $Dist = Join-Path $Root "dist"
New-Item -ItemType Directory -Force $Cache, $Dist | Out-Null
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
Write-Host "== Moj Portfel $Version (aktualizacje z repozytorium GitHub: '$UpdateRepo')"

# 1. Python embeddable (oficjalne archiwum python.org)
$Known = @{ "3.12.10" = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3" }   # suma zapisana przy pierwszym pobraniu z python.org (2026-11-12)
if (-not $PythonSha256 -and $Known.ContainsKey($PythonVersion) -and $Known[$PythonVersion] -ne "PLACEHOLDER") { $PythonSha256 = $Known[$PythonVersion] }
$Zip = Join-Path $Cache "python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path $Zip)) {
    Write-Host "Pobieram Python $PythonVersion (embeddable) z python.org"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $Zip -UseBasicParsing
}
$Hash = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLower()
if ($PythonSha256 -and $Hash -ne $PythonSha256.ToLower()) { Remove-Item $Zip; throw "Suma SHA-256 archiwum Pythona sie nie zgadza ($Hash)." }
Write-Host "Python embeddable SHA-256: $Hash"
Expand-Archive $Zip -DestinationPath (Join-Path $Stage "python")
$Pth = Get-ChildItem (Join-Path $Stage "python") -Filter "python3*._pth" | Select-Object -First 1
$Lib = ($Pth.Name -replace "\._pth$", "") + ".zip"
Set-Content $Pth.FullName -Value @($Lib, ".", "Lib\site-packages", "..\app", "import site") -Encoding ascii

# 2. Biblioteki: dokladnie wersje z requirements-lock.txt (bez dociagania innych), tylko paczki binarne dla Windows x64 / Python 3.12
$Site = Join-Path $Stage "python\Lib\site-packages"
& $HostPython -m pip install --disable-pip-version-check --no-warn-script-location --no-compile --target $Site `
    --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp --no-deps -r (Join-Path $Root "requirements-lock.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install nie powiodl sie." }
Get-ChildItem $Site -Directory -Filter "bin" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue   # skrypty konsolowe pip (niepotrzebne)

# 3. Kod aplikacji (bez plikow tymczasowych) + informacje o wydaniu do sprawdzania aktualizacji
$App = Join-Path $Stage "app"
robocopy (Join-Path $Root "invapp") (Join-Path $App "invapp") /E /XD __pycache__ /XF *.pyc *.mutbak _build.py /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Kopiowanie kodu nie powiodlo sie." }
$global:LASTEXITCODE = 0
Set-Content (Join-Path $App "invapp\_build.py") -Encoding utf8 -Value @(
    '"""Plik tworzony przy budowaniu instalatora (instalator/build.ps1) - nie edytowac."""',
    "UPDATE_REPO = `"$UpdateRepo`"",
    "BUILT_VERSION = `"$Version`"")

# 4. Kompilacja .pyc (szybszy pierwszy start), wbudowanym Pythonem
& (Join-Path $Stage "python\python.exe") -m compileall -q -j 0 $App $Site | Out-Null
$global:LASTEXITCODE = 0

# 5. Inno Setup
if (-not $Iscc) {
    $Cands = @((Join-Path $Build "tools\innosetup\ISCC.exe"), "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
    $Iscc = $Cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $Iscc) { throw "Nie znaleziono Inno Setup (ISCC.exe). Podaj -Iscc albo zainstaluj Inno Setup 6." }
& $Iscc /Q "/DAppVersion=$Version" "/DStageDir=$Stage" "/DOutputDir=$Dist" (Join-Path $PSScriptRoot "moj-portfel.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup zakonczyl sie bledem." }

$Exe = Join-Path $Dist "MojPortfel-Setup-$Version.exe"
$Sha = (Get-FileHash $Exe -Algorithm SHA256).Hash.ToLower()
Set-Content "$Exe.sha256" -Value "$Sha  MojPortfel-Setup-$Version.exe" -Encoding ascii
Set-Content (Join-Path $Dist "build.env") -Encoding ascii -Value @("APP_VERSION=$Version", "INSTALLER_NAME=MojPortfel-Setup-$Version.exe", "INSTALLER_SHA256=$Sha")
$Mb = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Write-Host "== Gotowe: $Exe ($Mb MB), SHA-256 $Sha"
