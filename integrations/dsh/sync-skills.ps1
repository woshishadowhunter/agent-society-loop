# Mirror the yogacara seed skills from the repository into a local skill root.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File integrations\dsh\sync-skills.ps1
#   powershell -ExecutionPolicy Bypass -File integrations\dsh\sync-skills.ps1 -Destination "$env:USERPROFILE\.agents\skills"
#
# DSH discovery roots (rank order): <project>/.dsh/skills, <project>/.agents/skills,
# <dshHome>/skills, <agentsHome>/skills. The repository .agents/skills is the
# source of truth; this script materializes it for other working directories.

param(
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$source = Join-Path $repo ".agents\skills"

if (-not (Test-Path $source)) {
    throw "skill source not found: $source"
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $dshHome = $env:DSH_HOME
    if ([string]::IsNullOrWhiteSpace($dshHome)) {
        $dshHome = Join-Path $env:USERPROFILE ".dsh"
    }
    $Destination = Join-Path $dshHome "skills"
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

$copied = 0
Get-ChildItem -Path $source -Directory | ForEach-Object {
    $target = Join-Path $Destination $_.Name
    Copy-Item -Path $_.FullName -Destination $target -Recurse -Force
    $copied++
    Write-Host ("synced skill: {0}" -f $_.Name)
}

Write-Host ("done: {0} skills -> {1}" -f $copied, $Destination)
