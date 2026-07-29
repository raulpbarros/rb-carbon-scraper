<#
.SYNOPSIS
    Build the Windows app: tests, slim database, EXE, portable ZIP, installer.

.DESCRIPTION
    Five steps, in this order and for a reason:

      1. pytest          — offline. A build that ships a broken export is
                           worse than no build, and this is the last point at
                           which that is cheap to find out.
      2. verra slim-db   — the database the build carries. Reads the real
                           data/verra.db, so whatever is in it right now is
                           what the business receives. Check `verra status`
                           first if that matters.
      3. pyinstaller     — one folder under dist/CarbonRegistryScraper.
      4. zip             — dist/portable/CarbonRegistryScraper-<v>-portable.zip.
                           THIS IS THE ONE TO HAND OUT. Neither the ZIP nor the
                           installer is signed, but a ZIP can be unblocked in
                           one click before anything is extracted, which is
                           what keeps SmartScreen out of the way. See the note
                           this script prints at the end, and LEIA-ME.txt.
      5. iscc            — wraps the same folder in a per-user installer, for
                           anyone who wants a Start-menu entry.

    Steps 3 and 5 need tools that are not project dependencies:

        pip install -e ".[build]"        # PyInstaller
        winget install JRSoftware.InnoSetup

.PARAMETER SkipTests
    Skip pytest. For iterating on the packaging itself, not for a build
    anyone else will run.

.PARAMETER SkipInstaller
    Stop after the portable ZIP. Useful when Inno Setup is not installed —
    the ZIP is the artifact that actually gets shared.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -SkipInstaller
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$build = Join-Path $root "build"

function Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Invoke-Native {
    <#
    Run an external program and fail on its exit code, not on its stderr.

    Windows PowerShell 5.1 wraps every stderr line from a native executable in
    an ErrorRecord, and with $ErrorActionPreference = "Stop" that is a
    terminating error. PyInstaller writes its entire INFO log to stderr, so a
    perfectly successful freeze aborts the build — but only when the output is
    redirected or piped, which is exactly the case nobody tests before handing
    the script to someone else. The exit code is the only honest signal.
    #>
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @(), [string]$What)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit $LASTEXITCODE)."
    }
}

function Write-SmartScreenNote {
    <#
    Printed by every path out of this script, because whoever runs the build
    is the person who sends the file, and the instruction below is the whole
    difference between a colleague opening the app and a colleague meeting a
    security warning on their first contact with it.
    #>
    Write-Host ""
    Write-Host "This build is unsigned. Windows marks anything downloaded from the" -ForegroundColor Yellow
    Write-Host "internet, and SmartScreen shows 'Windows protected your PC' for a" -ForegroundColor Yellow
    Write-Host "marked, unsigned program. Fixing that needs an Authenticode" -ForegroundColor Yellow
    Write-Host "certificate, not a code change." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "TELL WHOEVER RECEIVES THE ZIP, BEFORE THEY EXTRACT IT:" -ForegroundColor Yellow
    Write-Host "  right-click the .zip -> Properties -> tick 'Unblock' -> OK." -ForegroundColor Yellow
    Write-Host "That strips the mark once, and nothing extracted afterwards trips" -ForegroundColor Yellow
    Write-Host "SmartScreen at all. LEIA-ME.txt inside the ZIP says so in Portuguese." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "The same right-click-Unblock works on the setup .exe. The ZIP is" -ForegroundColor Yellow
    Write-Host "still the one to hand out: one unblock covers every file in it, and" -ForegroundColor Yellow
    Write-Host "there is no install step for a locked-down machine to object to." -ForegroundColor Yellow
}

# Prefer the virtualenv's interpreter, so a build never silently uses a
# different Python than the one the tests were written against.
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# One version string, read from pyproject.toml. The .iss carries its own copy
# because Inno needs it at compile time, and a test pins the two together — a
# third hardcoded copy here is how a ZIP labelled 0.2.0 ends up containing
# 0.3.0.
$readVersion = "import pathlib, sys, tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text('utf-8'))['project']['version'])"
$version = (& $python -c $readVersion (Join-Path $root "pyproject.toml")) | Select-Object -Last 1
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Could not read the version from pyproject.toml." }
$version = $version.Trim()

Step "Cleaning dist/ and build/"
foreach ($path in @($dist, $build)) {
    if (Test-Path $path) { Remove-Item $path -Recurse -Force }
}

if (-not $SkipTests) {
    Step "Running the test suite"
    Invoke-Native $python @("-m", "pytest", "-q") "Tests"
} else {
    Write-Warning "Tests skipped."
}

Step "Building the shipped database"
Invoke-Native $python @("-m", "carbon_scraper.cli", "slim-db", "--force") "slim-db"

$seed = Join-Path $dist "seed\carbon-seed.db"
if (-not (Test-Path $seed)) { throw "slim-db wrote nothing to $seed." }
$seedMb = [math]::Round((Get-Item $seed).Length / 1MB, 1)
Write-Host "    seed database: $seedMb MB"

Step "Freezing the application"
Invoke-Native $python @("-m", "PyInstaller", (Join-Path $root "packaging\carbon-registry.spec"), "--noconfirm") "PyInstaller"

$exe = Join-Path $dist "CarbonRegistryScraper\CarbonRegistryScraper.exe"
if (-not (Test-Path $exe)) { throw "No EXE at $exe." }

# The bundle is where the packaged/checkout path split gets broken silently,
# so check the read-only files actually made it in rather than finding out on
# the business team's machine.
$bundled = Join-Path $dist "CarbonRegistryScraper\_internal"
if (-not (Test-Path $bundled)) { $bundled = Join-Path $dist "CarbonRegistryScraper" }
foreach ($required in @("assets\fields-asked.txt", "config\credits.yaml",
                        "config\derivation\biome.yaml", "seed\carbon-seed.db")) {
    if (-not (Test-Path (Join-Path $bundled $required))) {
        throw "Missing from the bundle: $required"
    }
}
Write-Host "    bundle carries assets, config and the seed database."

Step "Packing the portable ZIP"

# The READ-ME travels inside the ZIP, next to the EXE. It is the only
# documentation the person who receives this will ever see, and step 1 of it —
# unblock before extracting — is what keeps SmartScreen out of their way.
$readme = Join-Path $root "packaging\LEIA-ME.txt"
if (-not (Test-Path $readme)) { throw "Missing $readme; the ZIP must carry it." }
Copy-Item $readme (Join-Path $dist "CarbonRegistryScraper\LEIA-ME.txt") -Force

$portableDir = Join-Path $dist "portable"
New-Item -ItemType Directory -Force $portableDir | Out-Null
$zip = Join-Path $portableDir "CarbonRegistryScraper-$version-portable.zip"

# CreateFromDirectory, not Compress-Archive: 56 MB of small files is minutes
# against seconds, and the last argument keeps `CarbonRegistryScraper\` as the
# top-level folder inside the archive, so extracting cannot scatter 300 files
# across whatever folder the user happened to be in.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    (Join-Path $dist "CarbonRegistryScraper"),
    $zip,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true)

$zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "    $zip  ($zipMb MB)"

if ($SkipInstaller) {
    Step "Done (installer skipped)"
    Write-Host "    $exe"
    Write-Host "    $zip"
    Write-SmartScreenNote
    return
}

Step "Building the installer"
$found = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($found) {
    $iscc = $found.Source
} else {
    # %LOCALAPPDATA% first: `winget install JRSoftware.InnoSetup` installs
    # per-user by default, which is where this build was verified. Inno does
    # not put ISCC.exe on PATH either way.
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) {
    throw "Inno Setup not found. Install it (winget install JRSoftware.InnoSetup) or pass -SkipInstaller."
}
Write-Host "    using $iscc"

Invoke-Native $iscc @((Join-Path $root "packaging\carbon-registry.iss")) "Inno Setup"

Step "Done"
Write-Host "    hand out:  $zip  ($zipMb MB)"
Get-ChildItem (Join-Path $dist "installer") -Filter *.exe | ForEach-Object {
    Write-Host ("    also:      {0}  ({1} MB)" -f $_.FullName, [math]::Round($_.Length / 1MB, 1))
}
Write-SmartScreenNote
