# demo_reset.ps1
# One-command rehearsal reset for the ToolForge demo.
# Backs up ~/.claude/toolforge.db, deletes it, restores PricingCard.jsx.

[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Fail([string]$msg) {
    Write-Host "demo_reset: $msg" -ForegroundColor Red
    exit 1
}

# Resolve paths
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scaffoldReset = Join-Path $scriptDir 'scaffold\reset.ps1'
$claudeDir = Join-Path $HOME '.claude'
$dbPath = Join-Path $claudeDir 'toolforge.db'

if (-not (Test-Path $scaffoldReset)) {
    Fail "cannot find scaffold reset script at $scaffoldReset"
}

# Confirmation prompt unless -Force
if (-not $Force) {
    $answer = Read-Host "Reset demo state? [y/n]"
    if ($answer -notmatch '^[Yy]') {
        Write-Host "demo_reset: aborted by user"
        exit 0
    }
}

# 1. Back up the DB if it exists
$backupSummary = "no existing toolforge.db to back up"
if (Test-Path $dbPath) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $backupPath = Join-Path $claudeDir "toolforge.db.bak-$ts"
    try {
        Copy-Item -Path $dbPath -Destination $backupPath -Force
    } catch {
        Fail "backup failed: $($_.Exception.Message)"
    }
    if (-not (Test-Path $backupPath)) {
        Fail "backup did not land at $backupPath, refusing to delete original"
    }
    $backupSummary = "backed up toolforge.db to $backupPath"

    # 2. Delete the DB only after the backup is verified on disk
    try {
        Remove-Item -Path $dbPath -Force
    } catch {
        Fail "delete failed: $($_.Exception.Message)"
    }
}

# 3. Run the scaffold reset script with -y (its skip-prompt flag)
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $scaffoldReset -y
    if ($LASTEXITCODE -ne 0) {
        Fail "scaffold reset.ps1 exited with code $LASTEXITCODE"
    }
} catch {
    Fail "scaffold reset failed: $($_.Exception.Message)"
}

# 4. Three-line summary
Write-Host ""
Write-Host "demo_reset: $backupSummary"
Write-Host "demo_reset: deleted ~/.claude/toolforge.db"
Write-Host "demo_reset: restored PricingCard.jsx to empty stub via scaffold/reset.ps1"
exit 0
