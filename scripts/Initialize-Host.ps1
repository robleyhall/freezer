[CmdletBinding()]
param([string]$Root = 'C:\freezer')
. "$PSScriptRoot/Common.ps1"
New-Item -ItemType Directory -Force -Path "$Root\cache", "$Root\runtime", "$Root\results" | Out-Null
if ((Get-WindowsFeature Hyper-V).InstallState -ne 'Installed') {
    Install-WindowsFeature -Name Hyper-V -IncludeManagementTools | Out-Null
    Write-Output 'REBOOT_REQUIRED'
    return
}
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) {
    Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0 | Out-Null
}
if (-not (Test-Path "$Root\runtime\python.exe")) {
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip' -OutFile "$Root\cache\python.zip"
    Expand-Archive -Path "$Root\cache\python.zip" -DestinationPath "$Root\runtime" -Force
    if ((Get-AuthenticodeSignature "$Root\runtime\python.exe").Status -ne 'Valid') {
        throw 'Python executable signature validation failed.'
    }
    # Embedded Python uses an explicit path list rather than the script directory.
    Add-Content -Path "$Root\runtime\python313._pth" -Value "$Root\lab" -Encoding ASCII
}
if (-not (Test-Path "$Root\cache\lab-key")) {
    & ssh-keygen.exe -t ed25519 -f "$Root\cache\lab-key" -N '""' -q
    if ($LASTEXITCODE -ne 0) { throw 'SSH key creation failed.' }
}
Write-Output 'HOST_READY'
