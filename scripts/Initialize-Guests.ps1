[CmdletBinding()]
param(
    [string]$Root = 'C:\freezer',
    [ValidateRange(50,10000)][int]$IntervalMs = 500,
    [ValidateRange(1,10)][int]$Fall = 2,
    [ValidateRange(1,10)][int]$Rise = 2,
    [ValidateRange(50,5000)][int]$CheckTimeoutMs = 300
)
. "$PSScriptRoot/Common.ps1"
Import-Module Hyper-V
$switchName = 'freezer-internal'
if (-not (Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue)) {
    New-VMSwitch -Name $switchName -SwitchType Internal | Out-Null
}
$adapter = Get-NetAdapter -Name "vEthernet ($switchName)"
if (-not (Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object IPAddress -eq '192.168.250.1')) {
    New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress 192.168.250.1 -PrefixLength 24 | Out-Null
}
if (-not (Get-NetNat -Name 'freezer-nat' -ErrorAction SilentlyContinue)) {
    New-NetNat -Name 'freezer-nat' -InternalIPInterfaceAddressPrefix '192.168.250.0/24' | Out-Null
}
$base = "$Root\cache\base.vhdx"
if (-not (Test-Path $base)) {
    $qemu = "$Root\qemu\qemu-img.exe"
    if (-not (Test-Path $qemu)) {
        $installer = "$Root\cache\qemu-setup.exe"
        $qemuBase = 'https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811'
        Invoke-WebRequest -UseBasicParsing "$qemuBase.exe" -OutFile $installer
        $expected = '5bcf9eed634e8575a37b74f445af41a2fe4106da512d0c30c368301d4c105037fdfab40a5287367a28a957624cddebbc8c07e16c88ab6634f554cdf3d16bf543'
        if ((Get-FileHash $installer -Algorithm SHA512).Hash -ne $expected) { throw 'QEMU installer checksum mismatch.' }
        $process = Start-Process -FilePath $installer -ArgumentList @('/S', "/D=$Root\qemu") -Wait -PassThru
        if ($process.ExitCode -ne 0 -or -not (Test-Path $qemu)) { throw 'qemu-img installation failed.' }
    }
    $imageName = 'ubuntu-24.04-server-cloudimg-amd64.img'
    $imageBase = 'https://cloud-images.ubuntu.com/releases/noble/release-20261017'
    # The generic .img is a bootable QCOW2 disk. The .tar.gz is only a filesystem.
    Invoke-WebRequest -UseBasicParsing "$imageBase/SHA256SUMS" -OutFile "$Root\cache\SHA256SUMS"
    $sumLine = Get-Content "$Root\cache\SHA256SUMS" | Where-Object { $_ -match " [*]?$([regex]::Escape($imageName))$" }
    if (@($sumLine).Count -ne 1) { throw 'Image checksum entry missing or ambiguous.' }
    $expectedHash = ($sumLine -split '\s+')[0]
    $image = "$Root\cache\$imageName"
    if (-not (Test-Path $image)) { Invoke-WebRequest -UseBasicParsing "$imageBase/$imageName" -OutFile $image }
    if ((Get-FileHash $image -Algorithm SHA256).Hash -ne $expectedHash) { throw 'Ubuntu image checksum mismatch.' }
    & $qemu check -f qcow2 $image
    if ($LASTEXITCODE -ne 0) { throw 'QCOW2 image validation failed.' }
    $converted = "$Root\cache\base-converted.vhdx"
    & $qemu convert -f qcow2 -O vhdx -o subformat=dynamic $image $converted
    if ($LASTEXITCODE -ne 0) { throw 'Guest disk conversion failed.' }
    # QEMU marks its Windows output sparse; Hyper-V cannot mount or resize it.
    $pending = "$Root\cache\base-pending.vhdx"
    if (Test-Path $pending) { Remove-Item -LiteralPath $pending }
    $source = [IO.File]::OpenRead($converted)
    try {
        $destination = [IO.File]::Open($pending, [IO.FileMode]::CreateNew)
        try { $source.CopyTo($destination, 1MB) }
        finally { $destination.Dispose() }
    }
    finally { $source.Dispose() }
    if ((Get-Item -LiteralPath $pending).Attributes -band [IO.FileAttributes]::SparseFile) {
        throw 'Guest disk is still sparse after copying.'
    }
    & $qemu compare -f qcow2 -F vhdx $image $pending
    if ($LASTEXITCODE -ne 0) { throw 'Converted guest disk differs from source.' }
    Remove-Item -LiteralPath $converted
    Resize-VHD -Path $pending -SizeBytes 8GB
    Move-Item -LiteralPath $pending -Destination $base
    Remove-Item -LiteralPath $image
}
$cfg = (Get-Content "$Root\lab\haproxy.cfg.in" -Raw).Replace('@INTERVAL@', "$IntervalMs").
    Replace('@FALL@', "$Fall").Replace('@RISE@', "$Rise").Replace('@CHECK_TIMEOUT@', "$CheckTimeoutMs")
Write-Utf8 "$Root\results\haproxy.cfg" $cfg
foreach ($number in 1,2) {
    $name = "freezer-app$number"
    $vm = Get-VM -Name $name -ErrorAction SilentlyContinue
    if ($vm) {
        if ($vm.Notes -ne 'freezer-lab') { throw "VM name collision: $name" }
        if ($vm.State -eq 'Off') { Start-VM -VM $vm }
        continue
    }
    $disk = "$Root\cache\$name.vhdx"
    Copy-Item $base $disk
    $seed = "$Root\cache\$name-seed.vhdx"
    New-VHD -Path $seed -SizeBytes 64MB -Dynamic | Out-Null
    $mounted = Mount-VHD -Path $seed -Passthru
    try {
        $partition = $mounted | Get-Disk | Initialize-Disk -PartitionStyle MBR -PassThru |
            New-Partition -UseMaximumSize -AssignDriveLetter
        $partition | Format-Volume -FileSystem FAT32 -NewFileSystemLabel cidata -Confirm:$false | Out-Null
        $drive = "$($partition.DriveLetter):\"
        $userData = (Get-Content "$Root\lab\guest.yaml.in" -Raw).
            Replace('@NAME@', $name).Replace('@BACKEND@', "app$number").
            Replace('@SSH_KEY@', (Get-Content "$Root\cache\lab-key.pub" -Raw).Trim()).
            Replace('@SERVICE@', [Convert]::ToBase64String([IO.File]::ReadAllBytes("$Root\lab\service.py"))).
            Replace('@HAPROXY@', [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cfg)))
        Write-Utf8 ($drive + 'user-data') $userData
        Write-Utf8 ($drive + 'meta-data') "instance-id: $name`nlocal-hostname: $name`n"
        Write-Utf8 ($drive + 'network-config') @"
version: 2
ethernets:
  eth0:
    match:
      name: 'e*'
    dhcp4: false
    addresses: [192.168.250.$(10 + $number)/24]
    routes:
      - to: default
        via: 192.168.250.1
    nameservers:
      addresses: [168.63.129.16]
"@
    }
    finally { Dismount-VHD -Path $seed }
    New-VM -Name $name -Generation 2 -MemoryStartupBytes 1GB -VHDPath $disk -SwitchName $switchName | Out-Null
    Set-VM -Name $name -Notes 'freezer-lab' -AutomaticCheckpointsEnabled $false -CheckpointType Disabled -AutomaticStopAction ShutDown
    Set-VMProcessor -VMName $name -Count 1
    Set-VMFirmware -VMName $name -EnableSecureBoot Off
    Add-VMHardDiskDrive -VMName $name -Path $seed
    Start-VM -Name $name
}
$deadline = (Get-Date).AddMinutes(20)
foreach ($number in 1,2) {
    do {
        $ready = $false
        try {
            $response = Invoke-RestMethod -Uri "http://192.168.250.$(10 + $number):8000/health" -TimeoutSec 3
            $ready = $response.backend -eq "app$number"
        }
        catch [System.Net.WebException] { Start-Sleep -Seconds 5 }
        if ((Get-Date) -gt $deadline) { throw "Guest app$number did not become ready; inspect cloud-init logs." }
    } until ($ready)
}
$proxy = Invoke-RestMethod -Uri 'http://192.168.250.12:8080/health' -TimeoutSec 5
if ($proxy.backend -notin @('app1','app2')) { throw 'HAProxy validation failed.' }
Write-Output 'GUESTS_READY'
