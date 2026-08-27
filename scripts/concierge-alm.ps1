[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Pack", "AssertReleaseSource", "Import")]
    [string]$Operation,

    [ValidateSet("TEST", "PROD")]
    [string]$Target,

    [string]$ArtifactPath,

    [string]$SettingsPath
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content (Join-Path $repoRoot "config\concierge\alm.yaml") -Raw

if ($config -notmatch "accepted_version:\s+1\.0\.0\.0") {
    throw "The accepted Concierge version is not pinned to 1.0.0.0."
}

$source = Join-Path $repoRoot "solutions\ContosoConcierge\src"
$acceptedArtifact = "ContosoConcierge_1_0_0_0_managed.zip"

if ($Operation -eq "AssertReleaseSource") {
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    python -m contoso_foundry.concierge_alm validate-release-source --path $source
    if ($LASTEXITCODE -ne 0) {
        throw "A release is blocked until the authorized DEV export contains the exact bound Concierge components."
    }
    exit
}

if ($Operation -eq "Pack") {
    if (-not $ArtifactPath) {
        $ArtifactPath = Join-Path $repoRoot "artifacts\ContosoConcierge_1_0_0_0_unmanaged.zip"
    }
    New-Item -ItemType Directory -Force (Split-Path -Parent $ArtifactPath) | Out-Null
    dotnet tool run pac solution pack --zipfile $ArtifactPath --folder $source --packagetype Unmanaged
    if ($LASTEXITCODE -ne 0) {
        throw "PAC failed to pack the unmanaged DEV source."
    }
    Get-FileHash -Algorithm SHA256 $ArtifactPath
    exit
}

if (-not $Target) {
    throw "Import requires -Target TEST or PROD."
}
if (-not $ArtifactPath -or (Split-Path -Leaf $ArtifactPath) -ne $acceptedArtifact) {
    throw "Import requires the pinned managed artifact $acceptedArtifact."
}
$env:PYTHONPATH = Join-Path $repoRoot "src"
python -m contoso_foundry.concierge_alm validate-package --path $ArtifactPath --managed true
if ($LASTEXITCODE -ne 0) {
    throw "Import requires the pinned managed ContosoConcierge solution."
}
if (-not $SettingsPath) {
    $SettingsPath = Join-Path $repoRoot "deployment\concierge\$($Target.ToLower()).settings.json"
}

function Assert-ConcreteValue {
    param(
        [string]$Name,
        [object]$Value
    )

    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value) -or $Value.StartsWith("<")) {
        throw "Deployment setting $Name is missing or still contains a placeholder."
    }
}

$settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json
$variables = @($settings.EnvironmentVariables)
$references = @($settings.ConnectionReferences)

foreach ($variable in $variables) {
    Assert-ConcreteValue -Name $variable.SchemaName -Value $variable.Value
}
foreach ($reference in $references) {
    Assert-ConcreteValue -Name "$($reference.LogicalName).ConnectionId" -Value $reference.ConnectionId
    Assert-ConcreteValue -Name "$($reference.LogicalName).ConnectorId" -Value $reference.ConnectorId
}

$version = $variables | Where-Object SchemaName -eq "ccs_AcceptedVersion"
$specialist = $variables | Where-Object SchemaName -eq "ccs_FoundrySpecialistBaseUrl"
$connection = $references | Where-Object LogicalName -eq "ccs_FoundrySpecialist"
if ($version.Value -ne "1.0.0.0" -or -not $specialist -or -not $connection) {
    throw "Deployment settings do not bind the accepted version and required specialist connection."
}

# Importing the solution does not publish the agent runtime or expose a channel.
dotnet tool run pac solution import --path $ArtifactPath --settings-file $SettingsPath
if ($LASTEXITCODE -ne 0) {
    throw "PAC failed to import the managed solution into $Target."
}
