param(
    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,

    [Parameter(Mandatory = $true)]
    [string]$ResourceGroupName,

    [Parameter(Mandatory = $true)]
    [string]$FunctionAppName,

    [Parameter(Mandatory = $true)]
    [string]$FoundryAccountName,

    [string]$WrapperFunctionName = "searcher_wrapper",
    [string]$WrapperRoute = "searcher-wrapper",
    [string]$KeyName = "foundry-wrapper-key"
)

$ErrorActionPreference = "Stop"

Write-Host "[1/4] Setting Azure subscription..."
az account set --subscription $SubscriptionId | Out-Null

Write-Host "[2/4] Reading Foundry managed identity..."
$foundryIdentityJson = az cognitiveservices account show `
    --resource-group $ResourceGroupName `
    --name $FoundryAccountName `
    --query "identity" `
    --output json

if (-not $foundryIdentityJson) {
    throw "Unable to read Foundry identity for $FoundryAccountName"
}

$foundryIdentity = $foundryIdentityJson | ConvertFrom-Json
if (-not $foundryIdentity.principalId) {
    throw "Foundry account does not have system-assigned managed identity enabled"
}

Write-Host "Foundry principalId: $($foundryIdentity.principalId)"

Write-Host "[3/4] Creating/updating dedicated key for wrapper..."
$functionKey = $null

try {
    $functionKeyJson = az functionapp function keys set `
        --resource-group $ResourceGroupName `
        --name $FunctionAppName `
        --function-name $WrapperFunctionName `
        --key-name $KeyName `
        --output json

    if ($functionKeyJson) {
        $functionKeysListJson = az functionapp function keys list `
            --resource-group $ResourceGroupName `
            --name $FunctionAppName `
            --function-name $WrapperFunctionName `
            --output json
        if ($functionKeysListJson) {
            $functionKeysObj = $functionKeysListJson | ConvertFrom-Json
            $functionKey = $functionKeysObj.PSObject.Properties[$KeyName].Value
        }
        if ($functionKey) {
            Write-Host "Function-level key created on function '$WrapperFunctionName'."
        }
    }
}
catch {
    Write-Host "Function-level key creation failed (likely function not deployed yet)."
}

if (-not $functionKey) {
    Write-Host "Falling back to host key..."
    $hostKeyJson = az functionapp keys set `
        --resource-group $ResourceGroupName `
        --name $FunctionAppName `
        --key-type functionKeys `
        --key-name $KeyName `
        --output json
    if ($hostKeyJson) {
        $hostKeysListJson = az functionapp keys list `
            --resource-group $ResourceGroupName `
            --name $FunctionAppName `
            --output json
        if ($hostKeysListJson) {
            $hostKeysObj = $hostKeysListJson | ConvertFrom-Json
            $functionKey = $hostKeysObj.functionKeys.PSObject.Properties[$KeyName].Value
        }
    }
    if ($functionKey) {
        Write-Host "Host-level function key created."
    }
}

if (-not $functionKey) {
    throw "Function key not returned for $WrapperFunctionName"
}

$wrapperUrl = "https://$FunctionAppName.azurewebsites.net/api/${WrapperRoute}?code=${functionKey}"

Write-Host "[4/4] Done"
Write-Host ""
Write-Host "Set these values for Foundry agent provisioning:"
Write-Host "FOUNDRY_SEARCHER_WRAPPER_URL=$wrapperUrl"
Write-Host ""
Write-Host "Optional (existing searcher agent):"
Write-Host "FOUNDRY_SEARCH_API_URL=https://$FunctionAppName.azurewebsites.net/api/search?code=<search-key>"
Write-Host ""
Write-Host "Security note: this enables key-based invocation."
Write-Host "If you want Entra ID-only invocation, configure Function App authsettingsV2 (EasyAuth) and allow Foundry MI audience/issuer."
