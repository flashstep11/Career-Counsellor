param(
  [string]$DeployRemote = "deploy",
  [string]$DeployRemoteBranch = "main",
  [string]$SourceBranch = "main",
  [switch]$PullOrigin,
  [switch]$AlwaysPush,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"

function Test-GitRef([string]$ref) {
  & git show-ref --verify --quiet $ref
  return ($LASTEXITCODE -eq 0)
}

function Exec-Git([string[]]$gitArgs) {
  $display = ($gitArgs | ForEach-Object {
      if ($_ -match '\s|\(|\)|\"') { '"' + ($_ -replace '"', '\\"') + '"' } else { $_ }
    }) -join ' '
  Write-Host "> git $display"
  & git @gitArgs
  if ($LASTEXITCODE -ne 0) {
    throw "git failed with exit code ${LASTEXITCODE}: git $display"
  }
}

$repoRoot = (& git rev-parse --show-toplevel 2>$null)
if (-not $repoRoot) {
  throw "Not inside a git repository."
}
Set-Location $repoRoot

# Safety checks
$dirty = (& git status --porcelain)
if ($dirty) {
  throw "Working tree is not clean. Commit/stash changes first."
}

$remotes = (& git remote)
if ($remotes -notcontains $DeployRemote) {
  throw "Remote '$DeployRemote' not found. Run: git remote add $DeployRemote <url>"
}

if ($PullOrigin) {
  Exec-Git @("fetch", "origin")
  Exec-Git @("pull", "--ff-only", "origin", $SourceBranch)
}

# Ensure source branch exists locally
$hasSource = Test-GitRef "refs/heads/$SourceBranch"
if (-not $hasSource) {
  Exec-Git @("fetch", "origin", $SourceBranch)
  Exec-Git @("checkout", $SourceBranch)
}

$currentBranch = (& git branch --show-current)
if (-not $currentBranch) {
  throw "Detached HEAD not supported by this script. Checkout a branch first."
}

if (-not $Yes) {
  Write-Host "This will FORCE UPDATE $DeployRemote/$DeployRemoteBranch with a new single-commit snapshot of '$SourceBranch'."
  Write-Host "It will NOT touch origin."
  $confirm = Read-Host "Type YES to continue"
  if ($confirm -ne "YES") {
    Write-Host "Aborted."
    exit 1
  }
}

# Create deploy-main orphan branch if missing
$deployLocalBranch = "deploy-main"
$hasDeployLocal = Test-GitRef "refs/heads/$deployLocalBranch"
if (-not $hasDeployLocal) {
  Exec-Git @("checkout", "--orphan", $deployLocalBranch)
  # Index is empty on orphan; read the source branch tree into index+working tree
  Exec-Git @("read-tree", "-u", "--reset", $SourceBranch)
  Exec-Git @("commit", "-m", "Deploy snapshot")
} else {
  Exec-Git @("checkout", $deployLocalBranch)
  Exec-Git @("read-tree", "-u", "--reset", $SourceBranch)

  # If no changes vs current snapshot commit, skip commit/push
  & git diff --cached --quiet
  $noChanges = ($LASTEXITCODE -eq 0)
  if ($noChanges) {
    if (-not $AlwaysPush) {
      Write-Host "No changes to snapshot; deploy branch already matches '$SourceBranch'."
      Exec-Git @("checkout", $currentBranch)
      exit 0
    }

    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Exec-Git @("commit", "--allow-empty", "-m", "Deploy snapshot ($stamp)")
  } else {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Exec-Git @("commit", "-m", "Deploy snapshot ($stamp)")
  }
}

# Force-update deploy/main
Exec-Git @("push", $DeployRemote, ("$deployLocalBranch`:$DeployRemoteBranch"), "--force")

# Return to original branch
Exec-Git @("checkout", $currentBranch)

Write-Host "Done. '$DeployRemote/$DeployRemoteBranch' now points to the latest snapshot commit."