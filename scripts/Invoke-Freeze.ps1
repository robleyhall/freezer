[CmdletBinding()]
param(
    [ValidateSet('freezer-app1')][string]$VMName = 'freezer-app1',
    [ValidateRange(1,60)][int]$DurationSeconds = 2,
    [Parameter(Mandatory)][string]$EventsPath,
    [Parameter(Mandatory)][string]$ExperimentId
)
. "$PSScriptRoot/Common.ps1"

function Write-Event([string]$Kind) {
    [ordered]@{ experiment = $ExperimentId; kind = $Kind; utc = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json -Compress | Add-Content -Path $EventsPath -Encoding UTF8
}

$vm = Get-VM -Name $VMName -ErrorAction Stop
if ($vm.State -ne 'Running') { throw 'Target must already be running.' }
if ($vm.Notes -ne 'freezer-lab') { throw 'Refusing to pause a VM not owned by this lab.' }
$taskName = 'freezer-resume-' + [Guid]::NewGuid().ToString('N')
$rescue = "Import-Module Hyper-V; if ((Get-VM -Name '$VMName').State -eq 'Paused') { Resume-VM -Name '$VMName' -ErrorAction Stop }"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -EncodedCommand ' +
    [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($rescue)))
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds($DurationSeconds + 30)
$null = Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -User SYSTEM -RunLevel Highest
$resumed = $false
try {
    Write-Event 'pause_requested'
    Suspend-VM -VM $vm -ErrorAction Stop
    if ((Get-VM -Name $VMName).State -ne 'Paused') { throw 'Pause was not confirmed.' }
    Write-Event 'paused'
    Start-Sleep -Seconds $DurationSeconds
}
finally {
    # Never let a full disk or failed log write prevent resume.
    if ((Get-VM -Name $VMName).State -eq 'Paused') { Resume-VM -Name $VMName -ErrorAction Stop }
    if ((Get-VM -Name $VMName).State -ne 'Running') { throw 'Resume failed; rescue task retained.' }
    $resumed = $true
    # Resume comes before all logging and cleanup, including errors writing the log.
    try { Write-Event 'resumed' }
    finally { if ($resumed) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false } }
}
