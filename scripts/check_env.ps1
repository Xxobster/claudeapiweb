# check_env.ps1 - read-only environment check for CPM (WP0).
# Prints versions and paths only. Never prints environment variable VALUES (only whether a key is set).
# Usage: powershell -NoProfile -File scripts\check_env.ps1 [-Json]
param([switch]$Json)
$ErrorActionPreference = "Continue"
$results = [ordered]@{}

function Get-ToolVersion {
    param([string]$Name, [string[]]$VersionArgs = @("--version"))
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) { return [ordered]@{ found = $false } }
    $out = $null
    try { $out = (& $cmd.Source @VersionArgs 2>&1 | Select-Object -First 1 | Out-String).Trim() } catch { $out = "error: $($_.Exception.Message)" }
    return [ordered]@{ found = $true; path = $cmd.Source; version = $out }
}

function Test-PathInfo {
    param([string]$Path)
    return [ordered]@{ path = $Path; exists = (Test-Path -LiteralPath $Path) }
}

$results.os = [ordered]@{
    caption = (Get-CimInstance Win32_OperatingSystem).Caption
    version = [Environment]::OSVersion.VersionString
}
$results.powershell = [ordered]@{ edition = $PSVersionTable.PSEdition; version = $PSVersionTable.PSVersion.ToString() }
$results.python = Get-ToolVersion "python"
$results.git    = Get-ToolVersion "git"
$results.gh     = Get-ToolVersion "gh"
$results.node   = Get-ToolVersion "node"
$results.pwsh   = Get-ToolVersion "pwsh"
$results.uv     = Get-ToolVersion "uv"
$results.claude = Get-ToolVersion "claude"
$results.bash   = Get-ToolVersion "bash"

$unity = "D:\Unity\Editors\6000.3.25f1\Editor\Unity.exe"
$blender = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
$results.unity = Test-PathInfo $unity
$results.blender = Test-PathInfo $blender
$results.blender_launcher = Test-PathInfo "C:\Program Files\Blender Foundation\Blender 5.2\blender-launcher.exe"
if ($results.blender.exists) {
    try { $results.blender.version = (& $blender --version 2>&1 | Select-Object -First 1 | Out-String).Trim() } catch { $results.blender.version = "error" }
}

$results.paths = [ordered]@{
    cpm_repo         = Test-PathInfo "D:\projects\ClaudeProjectManager"
    nirandyr_main    = Test-PathInfo "D:\projects\game"
    nirandyr_tools   = Test-PathInfo "D:\projects\game\tools"
    worktree_root    = Test-PathInfo "D:\projects\cpm-worktrees"
    build_lock       = Test-PathInfo "D:\projects\game-build\BUILD_RUNNING.lock"
    gpu_lock_dir     = Test-PathInfo "D:\projects\gpu-locks"
    devlock_py       = Test-PathInfo "D:\projects\game\tools\devices\devlock.py"
    onedrive_docs    = Test-PathInfo "C:\Users\xxobs\OneDrive\20 - PROJECTS\CLAUDE API WEB"
}
$results.cpm_repo_in_onedrive = ((Resolve-Path "D:\projects\ClaudeProjectManager").Path -match "OneDrive")

# Billing safety: report presence only, never the value.
$results.env = [ordered]@{
    ANTHROPIC_API_KEY_set    = [bool]$env:ANTHROPIC_API_KEY
    ANTHROPIC_AUTH_TOKEN_set = [bool]$env:ANTHROPIC_AUTH_TOKEN
}

$drives = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -in @("C", "D") }
$results.disk_free_gb = [ordered]@{}
foreach ($d in $drives) { $results.disk_free_gb[$d.Name] = [math]::Round($d.Free / 1GB, 1) }

if ($Json) { $results | ConvertTo-Json -Depth 5 } else {
    foreach ($k in $results.Keys) { "{0}: {1}" -f $k, (($results[$k] | ConvertTo-Json -Compress -Depth 5)) }
}
