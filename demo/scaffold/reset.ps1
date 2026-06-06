# Resets demo state between rehearsals.
# Backs up and clears ~/.claude/toolforge.db, restores the empty PricingCard stub.
# Pass -Force to skip the confirmation prompt.
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$scaffoldDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dbPath = Join-Path $HOME ".claude/toolforge.db"
$stub = Join-Path $scaffoldDir "src/PricingCard.template.jsx"
$target = Join-Path $scaffoldDir "src/PricingCard.jsx"

if (Test-Path $dbPath) {
    if (-not $Force) {
        $answer = Read-Host "About to back up and delete $dbPath. This contains your real ToolForge data. Continue? (y/N)"
        if ($answer -notmatch '^[yY]') {
            Write-Host "Aborted. No changes made." -ForegroundColor Yellow
            exit 1
        }
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$dbPath.bak-$stamp"
    Copy-Item $dbPath $backup
    Remove-Item $dbPath
    Write-Host "Backed up DB to $backup and cleared $dbPath" -ForegroundColor Green
} else {
    Write-Host "No toolforge.db found at $dbPath, skipping DB reset." -ForegroundColor DarkGray
}

if (-not (Test-Path $stub)) {
    Write-Host "Template stub missing at $stub. Cannot restore PricingCard.jsx." -ForegroundColor Red
    exit 1
}
Copy-Item $stub $target -Force
Write-Host "Restored PricingCard.jsx to empty stub." -ForegroundColor Green
Write-Host "Demo reset complete." -ForegroundColor Cyan
