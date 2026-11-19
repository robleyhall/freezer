param([string]$Root = 'C:\freezer')
. "$PSScriptRoot/Common.ps1"
foreach ($number in 1,2) {
    & ssh.exe -i "$Root\cache\lab-key" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$Root\cache\known_hosts" `
        "lab@192.168.250.$(10 + $number)" 'sudo systemctl restart freezer-http'
    if ($LASTEXITCODE -ne 0) { throw "HTTP service restart failed on app$number" }
}
& ssh.exe -i "$Root\cache\lab-key" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$Root\cache\known_hosts" `
    'lab@192.168.250.12' 'sudo haproxy -c -f /etc/haproxy/haproxy.cfg && sudo systemctl restart haproxy'
if ($LASTEXITCODE -ne 0) { throw 'HAProxy service restart failed.' }
