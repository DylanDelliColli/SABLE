# SABLE installer (PowerShell) — Windows guidance shim.
#
# SABLE's execution layer is the tmux warm-pane session (see
# TMUX-AGENTS-DESIGN.md): every agent role runs as a persistent `claude`
# session in its own tmux pane. tmux does not exist on native Windows, and the
# hook scripts are bash — so the supported Windows path is WSL.
#
# This shim exists so the documented Windows entry point fails loudly with
# instructions instead of half-installing an unusable layer.
#
# Run from the SABLE repo root:
#   pwsh ./install.ps1     (PowerShell 7+, recommended)
#   powershell ./install.ps1   (Windows PowerShell 5.1)

$ErrorActionPreference = 'Stop'

function Write-Bold($msg)   { Write-Host $msg -ForegroundColor White }
function Write-Yellow($msg) { Write-Host $msg -ForegroundColor Yellow }

Write-Bold 'SABLE installer (Windows)'
Write-Host ''
Write-Yellow 'SABLE requires a POSIX environment with tmux — its execution layer runs'
Write-Yellow 'every agent as a persistent claude session in a tmux pane, and its hooks'
Write-Yellow 'are bash scripts. Native Windows cannot run either.'
Write-Host ''
Write-Bold 'Install inside WSL instead:'
Write-Host '  1. Install WSL:            wsl --install          (then restart)'
Write-Host '  2. Inside your WSL distro: sudo apt install tmux  (or your package manager)'
Write-Host '  3. Clone this repo in WSL and run:  bash install.sh'
Write-Host ''
Write-Host 'Details: QUICKSTART.md (prerequisites) and TMUX-AGENTS-DESIGN.md (topology).'
exit 1
