# Test instalatora na CZYSTYM Windowsie (GitHub Actions: windows-latest, bez naszego Pythona i bibliotek):
# instalacja w trybie cichym -> aplikacja startuje sama (jak po aktualizacji) -> /zdrowie: wersja, baza i migracje -> pusta strona startowa
# (czysta instalacja nie ma zadnych danych) -> naglowki bezpieczenstwa -> zatrzymanie -> deinstalacja (tryb cichy NIE usuwa danych).
# Uzycie: powershell -ExecutionPolicy Bypass -File instalator\test_instalacji.ps1 -Installer dist\MojPortfel-Setup-0.3.0.exe -Version 0.3.0
param(
  [Parameter(Mandatory = $true)][string]$Installer,
  [Parameter(Mandatory = $true)][string]$Version
)
$ErrorActionPreference = "Stop"
$App = Join-Path $env:LOCALAPPDATA "Programs\MojPortfel"
$Data = Join-Path $env:LOCALAPPDATA "AplikacjaInwestycyjna"

function Fail($msg) {
  Write-Host "BLAD: $msg"
  if (Test-Path "$Data\logi") { Get-ChildItem "$Data\logi" -File | ForEach-Object { Write-Host "--- $($_.Name)"; Get-Content $_.FullName -Tail 60 } }
  exit 1
}

# bezpiecznik: test instaluje i ODINSTALOWUJE aplikacje - tylko na maszynie GitHub Actions, nigdy na komputerze uzytkownika
if ($env:GITHUB_ACTIONS -ne "true") { Write-Host "Ten test uruchamia sie tylko w GitHub Actions (odinstalowuje aplikacje)."; exit 2 }
if (Test-Path $Data) { Fail "katalog danych juz istnieje - to nie jest czysty system" }
Write-Host "1. Instalacja (tryb cichy): $Installer"
# bez -Wait: Start-Process -Wait czeka tez na procesy potomne, a instalator na koniec uruchamia aplikacje (dziala dalej) - czekamy tylko na instalator
$p = Start-Process -FilePath $Installer -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-" -PassThru
$null = $p.Handle
if (-not $p.WaitForExit(600000)) { Fail "instalator nie zakonczyl sie w 10 minut" }
if ($p.ExitCode -ne 0) { Fail "instalator zakonczyl sie kodem $($p.ExitCode)" }
if (-not (Test-Path "$App\python\pythonw.exe")) { Fail "brak $App\python\pythonw.exe" }
if (-not (Test-Path "$App\app\invapp\_build.py")) { Fail "brak pliku _build.py (repozytorium aktualizacji)" }

Write-Host "2. Aplikacja uruchamia sie sama po instalacji - czekam na plik stanu i odpowiedz serwera"
$port = $null
for ($i = 0; $i -lt 120 -and -not $port; $i++) {
  Start-Sleep -Seconds 2
  if (Test-Path "$Data\stan.json") {
    try {
      $st = Get-Content "$Data\stan.json" -Raw | ConvertFrom-Json
      $h = Invoke-RestMethod "http://127.0.0.1:$($st.port)/zdrowie" -TimeoutSec 3
      if ($h.status -eq "dziala") { $port = $st.port }
    } catch { }
  }
}
if (-not $port) { Fail "aplikacja nie odpowiada po 4 minutach" }
$h = Invoke-RestMethod "http://127.0.0.1:$port/zdrowie"
Write-Host ("   wersja {0}, baza ok={1}, migracje={2}, tryb={3}" -f $h.wersja, $h.baza.ok, $h.baza.last_migration, $st.tryb)
if ($h.wersja -ne $Version) { Fail "wersja $($h.wersja) zamiast $Version" }
if (-not $h.baza.ok) { Fail "baza danych nie dziala" }
$migr = (Get-ChildItem "$App\app\invapp\db\migrations\*.sql").Count
if ($h.baza.last_migration -ne $migr) { Fail "migracje: $($h.baza.last_migration) z $migr" }

Write-Host "3. Czysta instalacja nie ma zadnych danych"
$page = Invoke-WebRequest "http://127.0.0.1:$port/podglad" -UseBasicParsing
if ($page.Content -notmatch "Portfel jest jeszcze pusty") { Fail "strona startowa nie jest pusta" }
foreach ($hdr in "Content-Security-Policy", "X-Frame-Options") { if (-not $page.Headers[$hdr]) { Fail "brak naglowka $hdr" } }

Write-Host "4. Dostep tylko lokalny: obcy naglowek Host jest odrzucany"
$req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$port/api/zywo"); $req.Host = "evil.example"
$code = 0
try { $req.GetResponse().Close(); $code = 200 } catch [System.Net.WebException] { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } }
if ($code -ne 400) { Fail "obcy Host: oczekiwano 400, jest $code" }

Write-Host "5. Zatrzymanie aplikacji"
& "$App\python\python.exe" -m invapp --stop
Start-Sleep -Seconds 3
try { Invoke-RestMethod "http://127.0.0.1:$port/zdrowie" -TimeoutSec 3 | Out-Null; Fail "aplikacja nadal dziala po --stop" } catch { }

Write-Host "6. Deinstalacja (tryb cichy nie usuwa danych)"
$un = Get-ChildItem "$App\unins*.exe" | Select-Object -First 1
$p = Start-Process -FilePath $un.FullName -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -PassThru
$null = $p.Handle
if (-not $p.WaitForExit(300000)) { Fail "deinstalator nie zakonczyl sie w 5 minut" }
# deinstalator Inno uruchamia swoja kopie z katalogu tymczasowego i sam konczy sie od razu - czekamy na usuniecie plikow programu
for ($i = 0; $i -lt 120 -and (Test-Path "$App\python\python.exe"); $i++) { Start-Sleep -Seconds 2 }
if (Test-Path "$App\python\python.exe") { Fail "program nie zostal usuniety w 4 minuty" }
if (-not (Test-Path "$Data\baza")) { Fail "dane zostaly usuniete w trybie cichym" }
Write-Host "OK: instalacja, start, pusta baza, bezpieczenstwo, zatrzymanie i deinstalacja dzialaja na czystym Windowsie."
