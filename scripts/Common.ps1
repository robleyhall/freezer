Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Az {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $output = & az @Arguments --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI failed: $($Arguments[0..1] -join ' ')" }
    return ($output -join "`n")
}

function Invoke-LabHost {
    param([string]$Subscription, [string]$ResourceGroup, [string]$Script)
    $response = Invoke-Az -Arguments @('vm','run-command','invoke','--subscription',$Subscription,
        '-g',$ResourceGroup,'-n','freezer-host','--command-id','RunPowerShellScript',
        '--scripts',('$ErrorActionPreference = ''Stop''; ' + $Script),'-o','json') | ConvertFrom-Json
    $message = ($response.value | ForEach-Object { $_.message }) -join "`n"
    if ($message -match '\[stderr\]\s*\S') { throw $message }
    return $message
}

function Write-Utf8 {
    param([string]$Path, [string]$Content)
    [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding($false)))
}
