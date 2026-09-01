<#
.SYNOPSIS
    Mesh-Spy installer for Windows 10 and 11.

.DESCRIPTION
    Finds or installs Python, builds the virtualenv, writes the first config,
    and optionally starts Mesh-Spy at logon. Everything it does by default is
    per-user, so this does not need an Administrator prompt.

    The actual Python setup is delegated to bootstrap.py, which is the same
    code path Linux and macOS take. This script only adds the parts that are
    genuinely Windows-specific.

.PARAMETER NoAutoStart
    Skip the scheduled task that starts Mesh-Spy when you log in.

.PARAMETER Recreate
    Delete and rebuild the virtualenv from scratch.

.PARAMETER OpenFirewall
    Allow other machines on your LAN to reach the console. Needs
    Administrator, and is pointless unless you also enable auth and set
    server.host to 0.0.0.0 in config/config.yaml.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute(
    'PSAvoidUsingWriteHost', '',
    Justification = 'An interactive installer talking to the person running it. Write-Output here would also put these lines on the pipeline.'
)]
[CmdletBinding()]
param(
    [switch]$NoAutoStart,
    [switch]$Recreate,
    [switch]$OpenFirewall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'Mesh-Spy'
$Port = 8090

function Write-Step($Message) { Write-Host "[Mesh-Spy] $Message" -ForegroundColor Cyan }
function Write-Note($Message) { Write-Host "[Mesh-Spy] $Message" }
function Write-Warn($Message) { Write-Host "[Mesh-Spy] warning: $Message" -ForegroundColor Yellow }

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

# Runs a program and returns its stdout lines plus its exit code, without
# letting anything it prints to stderr become a terminating error.
#
# Two Windows-only traps are handled here rather than at each call site.
# First, while $ErrorActionPreference is Stop, a native command writing to
# stderr raises a terminating error, so an interpreter that merely printed a
# deprecation warning would be judged unusable. Second, $LASTEXITCODE has to be
# read before anything else runs, or it reports the wrong command.
function Invoke-Capture {
    param([string]$Exe, [string[]]$Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>$null
        $code = $LASTEXITCODE
    } catch {
        return @{ Ok = $false; Output = ''; Code = -1 }
    } finally {
        $ErrorActionPreference = $previous
    }
    $text = ($output | Out-String).Trim()
    return @{ Ok = ($code -eq 0); Output = $text; Code = $code }
}

# Anything handed to a native command must avoid embedded double quotes.
# PowerShell 5.1 strips them while building the command line, so
#   -c 'print("%d.%d" % sys.version_info[:2])'
# arrives at python as
#   print(%d.%d % sys.version_info[:2])
# which is a SyntaxError, and the installer concludes there is no Python at
# all. Encoding the version as one integer keeps every snippet quote-free.
$VersionProbe = 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])'
$ExecutableProbe = 'import sys; print(sys.executable)'

# meshtastic requires >=3.9,<3.15.
$MinVersion = 309
$MaxVersion = 315

# Windows ships a stub at WindowsApps\python.exe that opens the Microsoft Store
# instead of running Python. It answers `Get-Command python` perfectly happily,
# so anything that only checks for the command's existence picks it and then
# fails much later, inside pip, with an error that names none of this.
function Test-RealPython($Path) {
    if (-not $Path) { return $false }
    if ($Path -like '*WindowsApps*') { return $false }

    $result = Invoke-Capture -Exe $Path -Arguments @('-c', $VersionProbe)
    if (-not $result.Ok -or -not $result.Output) { return $false }

    $version = 0
    if (-not [int]::TryParse($result.Output, [ref]$version)) { return $false }
    return ($version -ge $MinVersion -and $version -lt $MaxVersion)
}

function Find-Python {
    # The py launcher first: it is the only one that reliably resolves to a
    # real interpreter rather than the Store alias, and it can pick a version.
    $launcher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($wanted in @('-3.12', '-3.11', '-3.13', '-3.10', '-3')) {
            $result = Invoke-Capture -Exe $launcher.Source `
                -Arguments @($wanted, '-c', $ExecutableProbe)
            if ($result.Ok -and $result.Output -and (Test-RealPython $result.Output)) {
                return $result.Output
            }
        }
    }

    foreach ($candidate in @(Get-Command 'python' -All -ErrorAction SilentlyContinue)) {
        if (Test-RealPython $candidate.Source) { return $candidate.Source }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command 'winget' -ErrorAction SilentlyContinue)) {
        # winget is absent from Windows Server and from Windows 10 images that
        # have never had the App Installer update, so this is a normal path
        # rather than an edge case, and the message has to be actionable.
        throw @"
No suitable Python found, and winget is not available to install one.

Install Python 3.12 from https://www.python.org/downloads/
During setup, tick "Add python.exe to PATH".

Then open a new terminal and run this script again.
"@
    }
    Write-Step 'No suitable Python found. Installing Python 3.12 via winget...'
    & winget install --id Python.Python.3.12 -e --source winget `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw 'winget could not install Python. Install it from https://www.python.org/downloads/ and re-run.'
    }

    # winget updates the machine PATH but not the PATH of a shell that is
    # already open, so without this the freshly installed python is invisible
    # until the user opens a new terminal.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# ---------------------------------------------------------------------------
# Autostart
# ---------------------------------------------------------------------------

function Install-AutoStart($PythonwExe) {
    # pythonw.exe rather than python.exe so logging in does not leave a console
    # window sitting on the desktop. The app notices it has no stderr and logs
    # to data\mesh-spy.log instead.
    Write-Step 'Registering the logon task so Mesh-Spy starts with Windows...'

    $action = New-ScheduledTaskAction -Execute $PythonwExe `
        -Argument '-m app.main' -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # Defaults that make sense for a desktop machine rather than a server: do
    # not stop after three days, and do not refuse to start on battery, which
    # would silently disable this on every laptop.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited

    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description 'Mesh-Spy unified Meshtastic and MeshCore console' | Out-Null

    Write-Note "Autostart installed. Manage it with: Start-ScheduledTask $TaskName"
}

function Install-FirewallRule {
    if (-not (Test-Administrator)) {
        Write-Warn 'Opening the firewall needs Administrator. Re-run an elevated shell with -OpenFirewall.'
        return
    }
    Write-Step "Allowing inbound TCP $Port on private networks..."
    Remove-NetFirewallRule -DisplayName 'Mesh-Spy console' -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName 'Mesh-Spy console' -Direction Inbound `
        -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
    Write-Warn 'The console is only reachable from the LAN once you also set'
    Write-Warn 'server.host to 0.0.0.0 and enable auth. It refuses to bind otherwise.'
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

Write-Step "Install directory: $Root"
Set-Location $Root

$python = Find-Python
if (-not $python) {
    Install-Python
    $python = Find-Python
    if (-not $python) {
        throw 'Python was installed but still cannot be found. Open a new terminal and re-run this script.'
    }
}
Write-Note "Using Python: $python"

$bootstrapArgs = @('bootstrap.py', '--setup-only')
if ($Recreate) { $bootstrapArgs += '--recreate' }

Write-Step 'Setting up the virtual environment and dependencies...'
& $python @bootstrapArgs
if ($LASTEXITCODE -ne 0) { throw 'bootstrap.py failed; see the output above.' }

$venvPython = Join-Path $Root '.venv\Scripts\python.exe'
$venvPythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $venvPython)) { throw "Expected an interpreter at $venvPython." }

if ($OpenFirewall) { Install-FirewallRule }

if (-not $NoAutoStart) {
    if (Test-Path $venvPythonw) {
        Install-AutoStart $venvPythonw
    } else {
        Write-Warn 'pythonw.exe is missing from the virtualenv; skipping autostart.'
    }
}

# ---------------------------------------------------------------------------
# What to do next
# ---------------------------------------------------------------------------

Write-Host ''
Write-Step 'Done.'
Write-Host ''
& $venvPython -m app.main --list-ports
Write-Host ''
Write-Note "Console:  http://127.0.0.1:$Port"
Write-Note "Config:   $(Join-Path $Root 'config\config.yaml')"
Write-Note "Logs:     $(Join-Path $Root 'data\mesh-spy.log')  (when started by the logon task)"
Write-Note 'With no radio configured the console shows a simulated network.'
Write-Note 'Transmitting is off by default (mesh.read_only: true) and needs auth.'
Write-Host ''
Write-Note 'Start it now:      .\run.bat        (or: python bootstrap.py)'
if (-not $NoAutoStart) {
    Write-Note "Start in background: Start-ScheduledTask $TaskName"
}
Write-Note 'Full guide:        docs\INSTALL.md'
