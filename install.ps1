# SABLE installer (PowerShell) — Windows native.
# Copies hooks, prepares settings.json snippet, prepends Prime Directives to CLAUDE.md.
# Idempotent: safe to re-run. Backs up before editing CLAUDE.md.
#
# IMPORTANT: SABLE hook scripts are bash scripts. They require bash to execute.
# On Windows that means Git Bash (bundled with Git for Windows) or WSL must be installed.
# This installer copies the files and configures paths; it does NOT install bash.
#
# Run from the SABLE repo root:
#   pwsh ./install.ps1     (PowerShell 7+, recommended)
#   powershell ./install.ps1   (Windows PowerShell 5.1)

$ErrorActionPreference = 'Stop'

$RepoDir       = $PSScriptRoot
$ClaudeDir     = Join-Path $env:USERPROFILE '.claude'
$HooksSrc      = Join-Path $RepoDir 'hooks'
$HooksDst      = Join-Path $ClaudeDir 'hooks'
$TemplateDir   = Join-Path $RepoDir 'templates'
$SettingsFile  = Join-Path $ClaudeDir 'settings.json'
$GlobalClaude  = Join-Path $ClaudeDir 'CLAUDE.md'
$PrimeTemplate = Join-Path $TemplateDir 'global-CLAUDE-prime.md'

function Write-Bold($msg)   { Write-Host $msg -ForegroundColor White }
function Write-Green($msg)  { Write-Host $msg -ForegroundColor Green }
function Write-Yellow($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Red($msg)    { Write-Host $msg -ForegroundColor Red }

Write-Bold 'SABLE installer (PowerShell)'
Write-Host "OS:         Windows ($([System.Environment]::OSVersion.VersionString))"
Write-Host "Repo:       $RepoDir"
Write-Host "Target dir: $ClaudeDir"
Write-Host ''

# 1. Verify bd is installed
Write-Bold 'Step 1/6: Verify bd is installed'
$bd = Get-Command bd -ErrorAction SilentlyContinue
if (-not $bd) { $bd = Get-Command bd.exe -ErrorAction SilentlyContinue }
if (-not $bd) {
    Write-Red '  bd is not on PATH.'
    Write-Yellow '  Canonical install instructions: https://github.com/steveyegge/beads#installation'
    Write-Yellow '  Windows: download the .exe from releases, or use Scoop/Chocolatey if a package exists.'
    Write-Yellow '           Confirm the install dir (or .exe) is on PATH after install.'
    Write-Host ''
    Write-Yellow '  Note: bd uses Dolt as its storage backend. `bd dolt push` will fail without dolt installed.'
    Write-Yellow '  Dolt install: https://docs.dolthub.com/introduction/installation'
    Write-Yellow '  After installing, re-run: pwsh ./install.ps1'
    exit 1
}
$bdVersion = (& $bd.Source version 2>$null | Select-Object -First 1)
if (-not $bdVersion) { $bdVersion = "$($bd.Name) (version check failed but binary found)" }
Write-Green "  $bdVersion"

# Dolt check — non-fatal warning since not every workflow uses bd dolt push
$dolt = Get-Command dolt -ErrorAction SilentlyContinue
if (-not $dolt) { $dolt = Get-Command dolt.exe -ErrorAction SilentlyContinue }
if (-not $dolt) {
    Write-Yellow '  Note: dolt not found on PATH. `bd dolt push` (used in session-close protocol) will fail.'
    Write-Yellow '  Install: https://docs.dolthub.com/introduction/installation (not required to finish this install)'
}
Write-Host ''

# 2. Verify bash is available (required to execute hook scripts)
Write-Bold 'Step 2/6: Verify bash is available'
$bash = Get-Command bash -ErrorAction SilentlyContinue
if (-not $bash) {
    Write-Yellow '  bash not found on PATH.'
    Write-Yellow '  SABLE hook scripts require bash. Install one of:'
    Write-Yellow '    - Git for Windows (bundles Git Bash) — https://git-scm.com/download/win'
    Write-Yellow '    - WSL2 — wsl --install in admin PowerShell'
    Write-Yellow '  Continuing install — but hooks will not run until bash is available.'
} else {
    Write-Green "  bash found: $($bash.Source)"
}
Write-Host ''

# 3. Verify Claude config dir exists
Write-Bold 'Step 3/6: Verify ~/.claude exists'
if (-not (Test-Path $ClaudeDir)) {
    Write-Yellow "  $ClaudeDir not found. Creating it."
    New-Item -ItemType Directory -Path $ClaudeDir | Out-Null
}
Write-Green '  OK'
Write-Host ''

# 4. Copy hooks
Write-Bold "Step 4/6: Copy hooks to $HooksDst"
if (-not (Test-Path $HooksDst)) { New-Item -ItemType Directory -Path $HooksDst | Out-Null }
Get-ChildItem -Path $HooksSrc -Filter '*.sh' | ForEach-Object {
    $dst = Join-Path $HooksDst $_.Name
    Copy-Item $_.FullName -Destination $dst -Force
    Write-Green "  $($_.Name)"
}
Write-Host ''

# 5. Prepend Prime Directives to CLAUDE.md (with backup)
Write-Bold "Step 5/6: Add Prime Directives to $GlobalClaude"
if (-not (Test-Path $PrimeTemplate)) {
    Write-Red "  Missing template: $PrimeTemplate"
    exit 1
}

$skip = $false
if (Test-Path $GlobalClaude) {
    $existing = Get-Content $GlobalClaude -Raw
    if ($existing -match 'Prime Directive') {
        Write-Yellow '  Prime Directive already present — skipping CLAUDE.md edit'
        $skip = $true
    }
}

if (-not $skip) {
    $primeContent = Get-Content $PrimeTemplate -Raw
    if (Test-Path $GlobalClaude) {
        $stamp = Get-Date -Format 'yyyyMMddHHmmss'
        $backup = "$GlobalClaude.bak.$stamp"
        Copy-Item $GlobalClaude $backup
        Write-Yellow "  Backed up existing CLAUDE.md to $backup"
        $existingContent = Get-Content $GlobalClaude -Raw
        ($primeContent + $existingContent) | Set-Content -Path $GlobalClaude -NoNewline
    } else {
        Copy-Item $PrimeTemplate $GlobalClaude
    }
    Write-Green '  Prime Directives prepended'
}
Write-Host ''

# 6. Print settings.json snippet
Write-Bold 'Step 6/6: Settings.json hook block'
Write-Host "Add the following block to $SettingsFile under the top-level 'hooks' key."
Write-Host "If you already have a 'hooks' key, merge carefully (don't overwrite existing entries)."
Write-Host ''

# Use forward slashes in paths — Claude Code accepts them on Windows and avoids JSON-escaping backslashes
$hookPath = ($HooksDst -replace '\\','/')

@"
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash $hookPath/tdd-evidence.sh", "timeout": 3000},
          {"type": "command", "command": "bash $hookPath/tdd-gate.sh", "timeout": 5000},
          {"type": "command", "command": "bash $hookPath/bead-description-gate.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [
          {"type": "command", "command": "bash $hookPath/tdd-remind.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Agent",
        "hooks": [
          {"type": "command", "command": "bash $hookPath/agent-tdd-enforce.sh", "timeout": 3000}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash $hookPath/bead-quality.sh", "timeout": 5000}
        ]
      }
    ],
    "SessionStart": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ],
    "PreCompact": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ]
  }
}
"@ | Write-Host

Write-Host ''
Write-Bold 'Install complete.'
Write-Host ''
Write-Host 'Next steps:'
Write-Host "  1. Paste the hook block above into $SettingsFile (merge with existing config)."
Write-Host '  2. In your project: bd init; bd hooks install'
Write-Host '  3. Open a fresh agent session and use the bootstrap prompt from QUICKSTART.md'
Write-Host '  4. Verify: see "Verify the install" section of QUICKSTART.md'
if (-not $bash) {
    Write-Host ''
    Write-Yellow '  REMINDER: hooks will not execute until bash is on PATH (Git Bash or WSL).'
}
