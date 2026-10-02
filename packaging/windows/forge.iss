; Inno-Setup-Skript für den Windows-Installer von forge (forge-setup.exe).
;
; Installiert das von PyInstaller gebaute dist\forge.exe pro Benutzer (keine
; Adminrechte nötig) nach %LOCALAPPDATA%\Programs\forge, trägt das Verzeichnis
; in den Benutzer-PATH ein und registriert einen Uninstaller
; (Einstellungen → Apps). Gebaut von der CI (.github/workflows/ci.yml):
;
;   iscc /DAppVersion=0.7.0 packaging\windows\forge.iss
;
; Ergebnis: dist\forge-setup.exe

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{6B0B6B8E-3C1D-4E8B-9C55-F0B6E2A1F0A7}
AppName=forge
AppVersion={#AppVersion}
AppPublisher=forge
DefaultDirName={localappdata}\Programs\forge
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=forge-setup
Compression=lzma2
SolidCompression=yes
ChangesEnvironment=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=forge {#AppVersion}

[Files]
Source: "..\..\dist\forge.exe"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; Benutzer-PATH um {app} erweitern (nur wenn noch nicht enthalten).
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;

procedure RemovePath(Dir: string);
var
  Paths, S: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    exit;
  { Mit führendem ';' arbeiten, damit auch der erste Eintrag passt. }
  S := ';' + Paths;
  P := Pos(';' + Uppercase(Dir), Uppercase(S));
  if P = 0 then
    exit;
  Delete(S, P, Length(Dir) + 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Copy(S, 2, MaxInt));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemovePath(ExpandConstant('{app}'));
end;
