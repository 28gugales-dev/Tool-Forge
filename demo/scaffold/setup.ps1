# Run this once per fresh checkout. Installs npm deps for the ToolForge demo scaffold.
$ErrorActionPreference = 'Stop'
$scaffoldDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scaffoldDir

Write-Host "Installing demo scaffold dependencies in $scaffoldDir" -ForegroundColor Cyan
try {
    npm install
    if ($LASTEXITCODE -ne 0) { throw "npm install exited with code $LASTEXITCODE" }
} catch {
    Write-Host "Setup failed: $_" -ForegroundColor Red
    Write-Host "Check that Node.js and npm are on PATH, then re-run setup.ps1." -ForegroundColor Yellow
    exit 1
}

Write-Host "Scaffold ready. Run 'npm run dev' to start the demo at http://localhost:5173" -ForegroundColor Green
