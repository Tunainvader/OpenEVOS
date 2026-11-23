# Create desktop shortcuts for the OpenEVOS app (uses the project's .venv; no console window).
#   powershell -ExecutionPolicy Bypass -File tools\create_shortcuts.ps1
#   powershell -ExecutionPolicy Bypass -File tools\create_shortcuts.ps1 -Relay https://openevos-relay.<you>.workers.dev
# -Relay also makes the real-stage shortcut's read-only MCP link reachable through that relay (ADR-0062).
param([string]$Relay = "")
# Start the app from these shortcuts. A copy started from inside a packaged (MSIX) app, such as a terminal in the
# Claude desktop app, gets that app's private copy of %LOCALAPPDATA%\OpenEVOS: its own settings, logs, MCP token and
# instance lock, none of which the shortcut's copy sees (2026-11-23: the plate chosen in one never reached the other).
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$ico = Join-Path $root "assets\openevos.ico"
if (-not (Test-Path $pyw)) { throw "missing $pyw - create the venv first: python -m venv .venv; .venv\Scripts\pip install -e .[ui]" }
if ($Relay -and -not $Relay.StartsWith("https://")) { throw "-Relay must be an https:// URL" }
$relayArg = if ($Relay) { " --mcp-relay $Relay" } else { "" }
$desk = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$items = @(
    @{ Name = "OpenEVOS"; Args = "-m openevos.ui";
       Desc = "OpenEVOS desktop app - simulated instrument (nothing is sent to hardware)" },
    @{ Name = "OpenEVOS (real stage)";
       Args = "-m openevos.ui --motor-telemetry auto --motor-control --speed-control --stage-log " +
              "--mono-camera flycapture2 --colour-camera ueye --illumination real --mcp" + $relayArg;
       Desc = "OpenEVOS on the real instrument: stage + F carriage (COM3), real cameras, real LED board, X/Y speed control, read-only MCP link" }
)
foreach ($i in $items) {
    $lnk = $shell.CreateShortcut((Join-Path $desk ($i.Name + ".lnk")))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = $i.Args
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$ico,0"
    $lnk.Description = $i.Desc
    $lnk.Save()
    Write-Output ("created " + (Join-Path $desk ($i.Name + ".lnk")))
}
