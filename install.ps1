$ErrorActionPreference = "Stop"

Write-Host "=== code-exec Windows Installer ===" -ForegroundColor Cyan

# 1. Verify Python
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
}

if (-not $pythonCmd) {
    Write-Error "Python 3 is not found in your PATH. Please install Python 3.8+ from https://python.org"
    exit 1
}

# 2. Target installation directory
$localDir = (Get-Item -Path "." -ErrorAction SilentlyContinue).FullName
$repoUrl = "https://github.com/antonino54/code_exec.git"

if (Test-Path ".\code_exec.py") {
    $installDir = $localDir
    Write-Host "Detected local repository at: $installDir" -ForegroundColor Green
} else {
    $installDir = Join-Path $env:LOCALAPPDATA "code-exec"
    if (Test-Path (Join-Path $installDir ".git")) {
        Write-Host "Updating repository in $installDir..." -ForegroundColor Yellow
        git -C $installDir pull --ff-only
    } else {
        Write-Host "Cloning code-exec repository to $installDir..." -ForegroundColor Cyan
        git clone --depth=1 $repoUrl $installDir
    }
}

# 3. Create runner wrapper in a persistent User PATH bin folder
$binDir = Join-Path $env:LOCALAPPDATA "Programs\code-exec\bin"
if (-not (Test-Path $binDir)) {
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
}

$cmdScript = @"
@echo off
python "$installDir\code_exec.py" %*
"@

$cmdPath = Join-Path $binDir "code-exec.cmd"
Set-Content -Path $cmdPath -Value $cmdScript -Encoding ASCII

Write-Host "Created executable wrapper at $cmdPath" -ForegroundColor Green

# 4. Check & add to User PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -split ';' -notcontains $binDir) {
    $newPath = "$userPath;$binDir"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$env:Path;$binDir"
    Write-Host "Added $binDir to User PATH." -ForegroundColor Green
}

Write-Host "`nInstallation successful! Restart PowerShell or run: code-exec --help" -ForegroundColor Cyan
