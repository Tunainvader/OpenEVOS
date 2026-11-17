# Create desktop shortcuts for the OpenEVOS app (uses the project's .venv; no console window).
#   powershell -ExecutionPolicy Bypass -File tools\create_shortcuts.ps1
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$ico = Join-Path $root "assets\openevos.ico"
if (-not (Test-Path $pyw)) { throw "missing $pyw - create the venv first: python -m venv .venv; .venv\Scripts\pip install -e .[ui]" }
$desk = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$items = @(
    @{ Name = "OpenEVOS"; Args = "-m openevos.ui";
       Desc = "OpenEVOS desktop app - simulated instrument (nothing is sent to hardware)" },
    @{ Name = "OpenEVOS (real stage)"; Args = "-m openevos.ui --motor-telemetry auto --motor-control";
       Desc = "OpenEVOS with the REAL X/Y stage (ADR-0024): wait 2 min after power-on, stage clear, hand near the switch" }
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
