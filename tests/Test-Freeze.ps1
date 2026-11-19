$ErrorActionPreference = 'Stop'
$global:vmState = 'Running'
$global:mode = 'success'
$global:registered = $false
$global:resumes = 0
function global:Get-VM { [CmdletBinding()]param($Name)
    [pscustomobject]@{ State=$global:vmState; Notes='freezer-lab' }
}
function global:Suspend-VM { [CmdletBinding()]param($VM)
    $global:vmState = 'Paused'
    if ($global:mode -eq 'pause-error') { throw 'Injected suspend failure after pause' }
}
function global:Resume-VM { [CmdletBinding()]param($Name)
    $global:resumes++
    if ($global:mode -eq 'resume-error') { throw 'Injected resume failure' }
    $global:vmState = 'Running'
}
function global:New-ScheduledTaskAction { param($Execute,$Argument) 'action' }
function global:New-ScheduledTaskTrigger { param([switch]$Once,$At) 'trigger' }
function global:Register-ScheduledTask { param($TaskName,$Action,$Trigger,$User,$RunLevel) $global:registered=$true }
function global:Unregister-ScheduledTask { param($TaskName,[switch]$Confirm) $global:registered=$false }
function global:Start-Sleep { param($Seconds) }
function global:Add-Content { param($Path,$Encoding,[Parameter(ValueFromPipeline)]$Value)
    process {
        if ($global:mode -eq 'log-error' -and $global:vmState -eq 'Paused') { throw 'Injected logging failure' }
        Microsoft.PowerShell.Management\Add-Content -Path $Path -Value $Value -Encoding $Encoding
    }
}
$temp = Join-Path ([IO.Path]::GetTempPath()) ('freezer-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $temp | Out-Null
try {
    foreach ($case in 'success','pause-error','log-error','resume-error') {
        $global:mode=$case; $global:vmState='Running'; $global:registered=$false; $global:resumes=0
        $failed=$false
        try { & "$PSScriptRoot/../scripts/Invoke-Freeze.ps1" -DurationSeconds 2 -EventsPath "$temp/$case.jsonl" -ExperimentId test }
        catch { $failed=$true }
        if ($global:resumes -ne 1) { throw "${case}: did not attempt resume exactly once" }
        if ($case -eq 'resume-error') {
            if (-not $global:registered) { throw 'Failed resume discarded rescue task' }
        } else {
            if ($global:vmState -ne 'Running') { throw "${case}: guest left paused" }
            if ($global:registered) { throw "${case}: successful resume left stale rescue task" }
        }
        if (($case -eq 'success') -eq $failed) { throw "${case}: wrong error outcome" }
        Write-Output "PASS $case"
    }
}
finally { Remove-Item -LiteralPath $temp -Recurse -Force }
