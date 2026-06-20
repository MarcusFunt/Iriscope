param(
    [switch]$Once
)

$ErrorActionPreference = "Stop"

$RepoDir = if ($env:IRISCOPE_REPO_DIR) { $env:IRISCOPE_REPO_DIR } else { (Get-Location).Path }
$RepoUrl = if ($env:IRISCOPE_REPO_URL) { $env:IRISCOPE_REPO_URL } else { git -C $RepoDir config --get remote.origin.url }
if (-not $RepoUrl) { $RepoUrl = "https://github.com/MarcusFunt/Iriscope.git" }
$Branch = if ($env:IRISCOPE_BRANCH) { $env:IRISCOPE_BRANCH } else { "main" }
$Interval = if ($env:IRISCOPE_WATCHDOG_INTERVAL_S) { [int]$env:IRISCOPE_WATCHDOG_INTERVAL_S } else { 300 }
if ($Interval -lt 10) { $Interval = 300 }
$Service = if ($env:IRISCOPE_DOCKER_SERVICE) { $env:IRISCOPE_DOCKER_SERVICE } else { "iriscope-host" }

function Write-WatchdogLog {
    param([string]$Message)
    Write-Host "[iriscope-docker-watchdog] $Message"
}

function Get-RemoteCommit {
    $line = git ls-remote $RepoUrl "refs/heads/$Branch"
    if (-not $line) { return $null }
    return ($line -split "\s+")[0]
}

function Invoke-Compose {
    param([string[]]$Args)
    docker compose @Args
}

function Update-And-Restart {
    param([string]$RemoteSha)

    Write-WatchdogLog "Updating $RepoDir to $RemoteSha."
    git -C $RepoDir remote set-url origin $RepoUrl
    git -C $RepoDir fetch --prune origin $Branch
    git -C $RepoDir checkout -B $Branch "origin/$Branch"
    git -C $RepoDir reset --hard "origin/$Branch"
    git -C $RepoDir clean -ffd

    Write-WatchdogLog "Rebuilding and restarting Docker service $Service."
    Invoke-Compose -Args @("-f", (Join-Path $RepoDir "docker-compose.yml"), "up", "-d", "--build", "--force-recreate", $Service)
}

function Test-Once {
    $remoteSha = Get-RemoteCommit
    if (-not $remoteSha) {
        Write-WatchdogLog "Could not resolve $RepoUrl $Branch; keeping current container."
        return
    }

    $localSha = git -C $RepoDir rev-parse HEAD
    if ($localSha -eq $remoteSha) {
        Write-WatchdogLog "Already current at $($localSha.Substring(0, 12))."
        return
    }

    Write-WatchdogLog "Local $($localSha.Substring(0, 12)) differs from GitHub $($remoteSha.Substring(0, 12))."
    Update-And-Restart -RemoteSha $remoteSha
}

do {
    try {
        Test-Once
    } catch {
        Write-WatchdogLog "Check failed: $($_.Exception.Message)"
    }
    if (-not $Once) {
        Start-Sleep -Seconds $Interval
    }
} while (-not $Once)
