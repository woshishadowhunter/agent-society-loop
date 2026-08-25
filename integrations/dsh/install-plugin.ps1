# Install dsh-yogacara-society into a DSH profile (one command).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File integrations\dsh\install-plugin.ps1
#   powershell -ExecutionPolicy Bypass -File integrations\dsh\install-plugin.ps1 -Profile headless
#
# Steps:
#   1. install the agent-society-loop Python package (editable, from this repo)
#   2. ensure @modusensus/dsh-mneme is installed (prerequisite of the patch)
#   3. add this plugin bundle to the profile (pnpm add + bundles reconcile)
#   4. mirror the six yogacara seed skills into ~/.dsh/skills
#   5. verify the composed config
#
# A DSH restart is required after installation for the new bundle layer
# (mcp-society) to load.

param(
    [string]$Profile = "web",
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$pluginDir = Join-Path $PSScriptRoot "plugin\dsh-yogacara-society"
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE ".dsh" }

function Resolve-DshBin {
    # Locate the dsh CLI entry (lib/bin.js). The .cmd shim mangles non-ASCII
    # arguments (e.g. Chinese paths), so we always invoke node directly.
    $shim = Get-Command dsh.cmd -ErrorAction SilentlyContinue
    if ($shim) {
        $shimDir = Split-Path $shim.Source
        $candidates = @(
            (Join-Path $shimDir "..\node_modules\@deepseek-ai\dsh\lib\bin.js"),
            (Join-Path $shimDir "..\..\node_modules\@deepseek-ai\dsh\lib\bin.js"),
            (Join-Path $shimDir "..\@deepseek-ai\dsh\lib\bin.js")
        )
        foreach ($candidate in $candidates) {
            $resolved = [System.IO.Path]::GetFullPath($candidate)
            if (Test-Path $resolved) { return $resolved }
        }
    }
    $fallback = "C:\Users\Administrator\AppData\Local\npm-cache\_npx\1e7f6d9597241db0\node_modules\@deepseek-ai\dsh\lib\bin.js"
    if (Test-Path $fallback) { return $fallback }
    throw "dsh CLI not found; install @deepseek-ai/dsh or pass the path"
}

function Invoke-Dsh([string[]]$DshArgs) {
    $bin = Resolve-DshBin
    & node $bin @DshArgs
    return $LASTEXITCODE
}

Write-Host "[1/5] installing agent-society-loop python package (editable)..."
python -m pip install -e $RepoRoot --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Write-Host "      ok"

Write-Host "[2/5] ensuring @modusensus/dsh-mneme is installed..."
$manifest = Get-Content (Join-Path $dshHome "profiles\$Profile\package.json") -Raw | ConvertFrom-Json
if (-not $manifest.dependencies.PSObject.Properties.Name -contains "@modusensus/dsh-mneme") {
    Invoke-Dsh @("plugin", "--profile", $Profile, "add", "@modusensus/dsh-mneme") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "mneme install failed" }
    Write-Host "      installed @modusensus/dsh-mneme"
} else {
    Write-Host "      already present"
}

Write-Host "[3/5] adding dsh-yogacara-society bundle..."
Invoke-Dsh @("plugin", "--profile", $Profile, "add", $pluginDir) | Out-Null
if ($LASTEXITCODE -ne 0) { throw "plugin add failed" }
Write-Host "      ok"

Write-Host "[4/5] mirroring seed skills to $dshHome\skills ..."
$skillDest = Join-Path $dshHome "skills"
New-Item -ItemType Directory -Force -Path $skillDest | Out-Null
Get-ChildItem (Join-Path $pluginDir "skills") -Directory | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $skillDest $_.Name) -Recurse -Force
}
Write-Host "      ok"

Write-Host "[5/5] verifying composed config..."
$dump = Join-Path $env:TEMP "dsh-dump-install.txt"
Invoke-Dsh @("--profile", $Profile, "--dump-config") 2>$null | Out-File $dump -Encoding utf8
$lines = Get-Content $dump
foreach ($probe in @("mcp-society", "reasoningEffort: 'off'", "dreamMaxTokens: 32768", "dreamModel: deepseek-chat")) {
    if ($lines | Select-String -Pattern $probe -Quiet) {
        Write-Host "      ok: $probe"
    } else {
        Write-Host "      WARN missing: $probe"
    }
}

Write-Host ""
Write-Host "Done. RESTART DSH for the new bundle layer (mcp-society) to load."
Write-Host "After restart, the model gains mcp__society__* tools and autoDream"
Write-Host "keeps consolidating with the tuned configuration."
