; Inno Setup script for the Carbon Registry Scraper.
;
;   iscc packaging\carbon-registry.iss
;
; Wraps the PyInstaller one-folder build in an installer the business team can
; run themselves. `build.ps1` runs both in order.
;
; PER-USER INSTALL, DELIBERATELY. PrivilegesRequired=lowest means no UAC
; prompt and no administrator, so nobody has to raise a ticket with IT to get
; the tool onto their machine. The cost is that it installs under
; %LOCALAPPDATA%\Programs rather than Program Files, which is the right trade
; for an internal tool.
;
; SMARTSCREEN: this installer is unsigned, so the first person to run it sees
; "Windows protected your PC" with the Run button hidden behind "More info".
; That is not fixable here — it needs an Authenticode certificate. Tell people
; to expect it; do not tell them to disable SmartScreen.

#define AppName "Carbon Registry Scraper"
#define AppVersion "0.2.0"
#define AppPublisher "Raul Barros"
#define AppExeName "CarbonRegistryScraper.exe"
#define SourceDir "..\dist\CarbonRegistryScraper"

[Setup]
; Never change AppId: it is how Windows recognises an upgrade of this program
; rather than a second copy of it.
AppId={{8E3F2A41-6C9D-4B72-9F1E-3A5D7C0B4E88}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=CarbonRegistryScraper-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
; The shipped database is the bulk of this. Say so before the progress bar
; sits still for a minute.
DiskSpanning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent

; NO [UninstallDelete] SECTION, AND THAT IS THE POINT.
;
; Everything the program writes lives in %LOCALAPPDATA%\CarbonRegistryScraper:
; the database, the spreadsheets delivered to the business, the log, and the
; user's own edits to fields-asked.txt and the derivation rules. An uninstall
; removes the program and leaves all of it. Reinstalling then picks the
; existing database back up — settings.seed_database() only writes the shipped
; copy when there is no database at all — so an upgrade never costs anyone a
; two-hour scrape.
;
; If a "remove my data too" option is ever wanted, it belongs behind a task the
; user has to tick, never as the default.
