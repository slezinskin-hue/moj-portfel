; Instalator "Mój Portfel" (Inno Setup 6). Budowanie: instalator\build.ps1 (lokalnie) albo GitHub Actions po tagu vX.Y.Z.
; * instalacja bez uprawnien administratora do %LOCALAPPDATA%\Programs\MojPortfel (program); dane uzytkownika osobno w
;   %LOCALAPPDATA%\AplikacjaInwestycyjna (baza, kopie, logi) - instalacja nowej wersji ani deinstalacja ich nie usuwaja;
; * aktualizacja = ten sam instalator nowej wersji uruchomiony na starej (AppId sie nie zmienia); najpierw zatrzymuje dzialajaca aplikacje.
#define AppName "Mój Portfel"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\build\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{8C1F6A52-3E7B-4C3D-9A61-5B2E7D4F9A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\MojPortfel
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=MojPortfel-Setup-{#AppVersion}
SetupIconFile=ikona.ico
UninstallDisplayIcon={app}\ikona.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"

[Tasks]
Name: "desktopicon"; Description: "Utwórz skrót na pulpicie"; GroupDescription: "Skróty:"

[InstallDelete]
; stare pliki programu (np. usuniete moduly) nie moga zostac obok nowych - dane uzytkownika sa w innym katalogu
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "ikona.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m invapp"; WorkingDir: "{app}\app"; IconFilename: "{app}\ikona.ico"; Comment: "Mój Portfel"; AppUserModelID: "MojPortfel.App"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m invapp"; WorkingDir: "{app}\app"; IconFilename: "{app}\ikona.ico"; AppUserModelID: "MojPortfel.App"; Tasks: desktopicon

[Run]
; zwykla instalacja: pytanie "Uruchom" na koncu; aktualizacja z aplikacji (tryb cichy): aplikacja otwiera sie ponownie sama (we wlasnym oknie)
Filename: "{app}\python\pythonw.exe"; Parameters: "-m invapp"; WorkingDir: "{app}\app"; Description: "Uruchom {#AppName}"; Flags: postinstall nowait skipifsilent
Filename: "{app}\python\pythonw.exe"; Parameters: "-m invapp"; WorkingDir: "{app}\app"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: "-m invapp --stop"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "ZatrzymajAplikacje"

[Messages]
polish.FinishedLabel=Instalacja zakończona. Aplikacja otwiera się we własnym oknie (działa tylko na tym komputerze). Twoje dane są w %LOCALAPPDATA%\AplikacjaInwestycyjna i nie są usuwane przy aktualizacji ani deinstalacji.

[Code]
// Przed instalacja na starej wersji: czyste zatrzymanie dzialajacej aplikacji (serwer WWW i baza) - pliki nie sa wtedy zablokowane.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Rc: Integer;
  Py: String;
begin
  Result := '';
  Py := ExpandConstant('{app}\python\python.exe');
  if FileExists(Py) then
    Exec(Py, '-m invapp --stop', ExpandConstant('{app}\app'), SW_HIDE, ewWaitUntilTerminated, Rc);
end;
