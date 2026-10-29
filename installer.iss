; Instalador de AutoDirector Pro (se compila con Inno Setup 6)
#define MyAppName "AutoDirector Pro"
#define MyAppFull "AutoDirector Pro | Hablemos Entonces Producciones"
#define MyShortcut "AutoDirector Pro - Hablemos Entonces Producciones"
#define MyPublisher "Hablemos Entonces Producciones"
; La versión la pasa build_exe.bat (/DMyVersion=...). Este valor es solo de respaldo.
#ifndef MyVersion
  #define MyVersion "1.0.0"
#endif
#define MyExe "AutoDirectorPro.exe"

[Setup]
AppId={{B7C2D0A4-5E1F-4C57-9A3B-7D2E6F8A1C90}
AppName={#MyAppName}
AppVerName={#MyAppFull}
AppVersion={#MyVersion}
AppPublisher={#MyPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyShortcut}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=Instalador-AutoDirectorPro
SetupIconFile=icon.ico
; Logo en las pantallas del instalador (Inno elige el tamaño según la pantalla)
WizardImageFile=wizard_large.bmp,wizard_large_144.bmp,wizard_large_192.bmp
WizardSmallImageFile=wizard_small.bmp,wizard_small_144.bmp,wizard_small_192.bmp
UninstallDisplayIcon={app}\{#MyExe}
UninstallDisplayName={#MyAppFull}
VersionInfoVersion={#MyVersion}.0
VersionInfoDescription={#MyAppFull}
VersionInfoCompany={#MyPublisher}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Se instala solo para el usuario actual: no pide permisos de administrador
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Al actualizar, cierra el programa si sigue abierto
CloseApplications=yes

; El instalador pregunta el idioma al empezar (Español / English)
[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\{#MyExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyShortcut}"; Filename: "{app}\{#MyExe}"
Name: "{autodesktop}\{#MyShortcut}"; Filename: "{app}\{#MyExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
