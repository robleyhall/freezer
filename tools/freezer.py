"""Linux/WSL entry point. Windows-only Hyper-V operations run remotely."""
import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.local' / 'deployment.json'
TAGS = {'repo': 'freezer', 'project': 'freeze-lab', 'deployment': 'freezer-lab',
        'environment': 'disposable-lab'}
CANDIDATES = ['Standard_D2s_v4', 'Standard_D2ds_v4', 'Standard_D2s_v5', 'Standard_D4s_v4']


def az(*arguments, env=None):
    result = subprocess.run(['az', *map(str, arguments), '--only-show-errors', '-o', 'json'],
                            capture_output=True, text=True, env=env)
    if result.returncode:
        # Never echo command arguments: Run Command scripts can contain short-lived SAS tokens.
        raise RuntimeError(f'Azure CLI {" ".join(arguments[:2])} failed: {result.stderr.strip()}')
    return json.loads(result.stdout) if result.stdout.strip() else None


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def load_state():
    if not STATE.exists():
        raise RuntimeError('No deployment state. Run ./freezer deploy first.')
    return json.loads(STATE.read_text())


def save_state(state):
    STATE.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(STATE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream, indent=2)


def scoped(state):
    return ['--subscription', state['subscription'], '-g', state['resource_group']]


def host(state, script):
    # Run Command can return a successful Azure operation with a failed guest script.
    wrapped = ("$ErrorActionPreference='Stop'; try { " + script +
               "; Write-Output 'FREEZER_COMMAND_OK' } catch { Write-Output ('FREEZER_COMMAND_FAILED: ' + "
               "$_.Exception.Message); exit 1 }")
    response = az('vm', 'run-command', 'invoke', *scoped(state), '-n', 'freezer-host',
                  '--command-id', 'RunPowerShellScript', '--scripts', wrapped)
    text = '\n'.join(item.get('message', '') for item in response.get('value', []))
    if 'FREEZER_COMMAND_OK' not in text or 'FREEZER_COMMAND_FAILED:' in text:
        # Do not repeat the raw Azure response, which may include signed URLs.
        clean = re.sub(r'https://\S+', '[redacted-url]', text)
        raise RuntimeError('Remote command failed: ' + clean[-3000:])
    return text


def retail(region):
    # Only public SKU/pricing fields leave the machine; no subscription identifiers.
    quoted = ' or '.join(f"armSkuName eq '{sku}'" for sku in CANDIDATES)
    query = (f"armRegionName eq '{region}' and serviceName eq 'Virtual Machines' "
             f"and ({quoted}) and priceType eq 'Consumption'")
    url = 'https://prices.azure.com/api/retail/prices?' + urllib.parse.urlencode({'$filter': query})
    items = []
    while url:
        with urllib.request.urlopen(url, timeout=60) as response:
            document = json.load(response)
        items.extend(item for item in document['Items'] if 'Windows' in item['productName']
                     and not any(word in item['skuName'] for word in ('Spot', 'Low Priority'))
                     and item['isPrimaryMeterRegion'] and item['unitOfMeasure'] == '1 Hour')
        url = document.get('NextPageLink')
    return items


def preflight(subscription, region):
    if not region.isalnum():
        raise ValueError('Region must be an Azure region name')
    account = az('account', 'show', '--subscription', subscription)
    subscription = account['id']
    skus = az('vm', 'list-skus', '--subscription', subscription, '--location', region,
              '--resource-type', 'virtualMachines', '--all')
    available = {sku['name']: sku for sku in skus if sku['name'] in CANDIDATES
                 and not any(r['type'] == 'Location' for r in sku.get('restrictions', []))}
    usage = {row['name']['value'].lower(): row for row in
             az('vm', 'list-usage', '--subscription', subscription, '--location', region)}
    prices = retail(region)
    choices = []
    for item in prices:
        sku = available.get(item['armSkuName'])
        if not sku:
            continue
        caps = {cap['name']: cap['value'] for cap in sku['capabilities']}
        cores = int(caps['vCPUs'])
        family = usage.get(sku['family'].lower())
        total = usage.get('cores')
        if not family or not total or any(int(q['currentValue']) + cores > int(q['limit']) for q in (family, total)):
            continue
        choices.append({'size': sku['name'], 'vcpu': cores, 'memory_gib': float(caps['MemoryGB']),
                        'vm_hourly_usd': item['retailPrice']})
    if not choices:
        raise RuntimeError('No candidate has both regional availability and quota. No resources created.')
    choice = min(choices, key=lambda row: (row['vm_hourly_usd'], row['size']))
    image = az('vm', 'image', 'show', '--subscription', subscription, '--location', region,
               '--urn', 'MicrosoftWindowsServer:WindowsServer:2022-datacenter-smalldisk-g2:latest')
    choice.update(region=region, image_version=image['name'], currency='USD',
                  estimated_hourly_usd=round(choice['vm_hourly_usd'] + 0.005 + 1.536 / 730, 4),
                  estimate_note='Includes public IP and 32 GiB HDD; excludes transactions, bandwidth, tax.',
                  checked_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    return subscription, choice


def sync(state):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
        for directory in ('scripts', 'lab'):
            for path in sorted((ROOT / directory).rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:
                    archive.write(path, path.relative_to(ROOT))
    data = bundle.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    encoded = base64.b64encode(data).decode('ascii')
    # Avoid a shell's per-argument size limit. Each transfer stays below 24 KiB.
    host(state, "New-Item -ItemType Directory -Force C:\\freezer | Out-Null; "
         "[IO.File]::WriteAllText('C:\\freezer\\transfer.b64', '')")
    for offset in range(0, len(encoded), 24000):
        host(state, "[IO.File]::AppendAllText('C:\\freezer\\transfer.b64', "
             f"'{encoded[offset:offset + 24000]}')")
    host(state, "New-Item -ItemType Directory -Force C:\\freezer | Out-Null; "
         "[IO.File]::WriteAllBytes('C:\\freezer\\lab.zip', "
         "[Convert]::FromBase64String([IO.File]::ReadAllText('C:\\freezer\\transfer.b64'))); "
         f"if ((Get-FileHash C:\\freezer\\lab.zip -Algorithm SHA256).Hash -ne '{digest}') {{ throw 'Bundle checksum mismatch' }}; "
         "Expand-Archive C:\\freezer\\lab.zip -DestinationPath C:\\freezer -Force")
    print('Code transferred and checksum verified.', flush=True)


def check_tags(state):
    # ARM does not inherit VM tags onto the implicit OS disk; tag it explicitly.
    disk = az('disk', 'show', *scoped(state), '-n', 'freezer-os')
    az('tag', 'update', '--subscription', state['subscription'], '--resource-id', disk['id'],
       '--operation', 'Merge', '--tags', *[f'{k}={v}' for k, v in TAGS.items()])
    resources = az('resource', 'list', *scoped(state))
    bad = [row['type'] for row in resources if any((row.get('tags') or {}).get(k) != v for k, v in TAGS.items())]
    if bad:
        raise RuntimeError('Missing required tags on: ' + ', '.join(bad))


def deploy(args):
    prior = load_state() if STATE.exists() else None
    if prior and not args.resume:
        raise RuntimeError('Deployment state exists. Use deploy --resume to repair this exact owned lab.')
    subscription, estimate = preflight(args.subscription, args.region)
    print(json.dumps(estimate, indent=2), flush=True)
    if not args.approve_costs or estimate['estimated_hourly_usd'] > args.max_hourly_usd:
        raise RuntimeError('Explicit --approve-costs and sufficient --max-hourly-usd required. No resources created.')
    if not args.resource_group.startswith('freezer-lab'):
        raise RuntimeError('Resource group must start with freezer-lab')
    exists = az('group', 'exists', '--subscription', subscription, '-n', args.resource_group)
    if exists:
        if not prior or prior['subscription'] != subscription or prior['resource_group'] != args.resource_group:
            raise RuntimeError('Refusing to reuse an existing resource group without matching local ownership state')
        group = az('group', 'show', '--subscription', subscription, '-n', args.resource_group)
        if any((group.get('tags') or {}).get(k) != v for k, v in TAGS.items()):
            raise RuntimeError('Existing group ownership tags do not match')
    az('bicep', 'build', '--file', ROOT / 'infra/main.bicep', '--stdout')
    state = {'subscription': subscription, 'resource_group': args.resource_group,
             'region': args.region, 'estimate': estimate, 'stage': 'creating'}
    save_state(state)  # Keep a cleanup handle even when a deployment fails.
    az('group', 'create', '--subscription', subscription, '-n', args.resource_group,
       '-l', args.region, '--tags', *[f'{k}={v}' for k, v in TAGS.items()])
    password = 'Frz!' + secrets.token_urlsafe(27) + '8aA'
    parameters = {'$schema': 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#',
                  'contentVersion': '1.0.0.0', 'parameters': {
                      'adminPassword': {'value': password}, 'vmSize': {'value': estimate['size']},
                      'imageVersion': {'value': estimate['image_version']}}}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', dir=STATE.parent) as stream:
        json.dump(parameters, stream)
        stream.flush()
        result = az('deployment', 'group', 'create', *scoped(state), '-n', 'freezer',
                    '--template-file', ROOT / 'infra/main.bicep', '--parameters', '@' + stream.name)
    state['stage'] = 'deployed'
    save_state(state)
    check_tags(state)
    print('Infrastructure created. No inbound ports are open.', flush=True)
    configure(args, state)


def configure(args, state=None):
    state = state or load_state()
    sync(state)
    output = host(state, '& C:\\freezer\\scripts\\Initialize-Host.ps1')
    if 'REBOOT_REQUIRED' in output:
        az('vm', 'restart', *scoped(state), '-n', 'freezer-host')
        time.sleep(20)
        output = host(state, '& C:\\freezer\\scripts\\Initialize-Host.ps1')
    if 'HOST_READY' not in output:
        raise RuntimeError('Windows host initialization did not complete')
    output = host(state, f'& C:\\freezer\\scripts\\Initialize-Guests.ps1 -IntervalMs {args.interval_ms} '
                  f'-Fall {args.fall} -Rise {args.rise} -CheckTimeoutMs {args.check_timeout_ms}')
    if 'GUESTS_READY' not in output:
        raise RuntimeError('Nested guests did not become ready')
    state.update(stage='ready', interval_ms=args.interval_ms, fall=args.fall, rise=args.rise,
                 check_timeout_ms=args.check_timeout_ms)
    save_state(state)
    print('Windows Hyper-V host, both HTTP services and HAProxy are ready.', flush=True)


def run(args):
    state = load_state()
    if state['stage'] != 'ready':
        raise RuntimeError('Run configure first')
    sync(state)
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    state['last_run'] = run_id
    save_state(state)
    command = (f'& C:\\freezer\\runtime\\python.exe C:\\freezer\\lab\\experiment.py '
               f'--output C:\\freezer\\results\\{run_id} --repeats {args.repeats} '
               f'--interval-ms {state["interval_ms"]} --fall {state["fall"]} --rise {state["rise"]}; '
               "if ($LASTEXITCODE -ne 0) { throw 'Experiment failed; collect partial results for diagnosis' }; "
               f'& C:\\freezer\\runtime\\python.exe C:\\freezer\\lab\\report.py C:\\freezer\\results\\{run_id}; '
               "if ($LASTEXITCODE -ne 0) { throw 'Report generation failed' }")
    print('Running the 12-case Hyper-V matrix. This takes several minutes.', flush=True)
    try:
        print(host(state, command), flush=True)
    finally:
        collect(args, state)


def collect(args, state=None):
    state = state or load_state()
    run_id = state.get('last_run')
    if not run_id:
        raise RuntimeError('No experiment has been started')
    remote = f'C:\\freezer\\results\\{run_id}.zip'
    message = host(state, f"Compress-Archive -Path C:\\freezer\\results\\{run_id}\\* -DestinationPath {remote} -Force; "
                   f"$bytes = [IO.File]::ReadAllBytes('{remote}'); "
                   "Write-Output ('FREEZER_META:' + (@{length=[Convert]::ToBase64String($bytes).Length; "
                   f"sha256=(Get-FileHash '{remote}' -Algorithm SHA256).Hash" + "} | ConvertTo-Json -Compress))")
    match = re.search(r'FREEZER_META:(\{[^\r\n]+\})', message)
    if not match:
        raise RuntimeError('Result archive metadata missing')
    metadata = json.loads(match.group(1))
    partial = STATE.parent / f'{run_id}.b64'
    meta_path = partial.with_suffix('.json')
    if not meta_path.exists() or json.loads(meta_path.read_text()) != metadata:
        partial.write_text('')
        meta_path.write_text(json.dumps(metadata))
    encoded = partial.read_text()
    while len(encoded) < metadata['length']:
        offset = len(encoded)
        count = min(3000, metadata['length'] - offset)
        message = host(state, f"$data=[Convert]::ToBase64String([IO.File]::ReadAllBytes('{remote}')); "
                       f"Write-Output ('FREEZER_CHUNK:' + $data.Substring({offset},{count}))")
        match = re.search(r'FREEZER_CHUNK:([A-Za-z0-9+/=]+)', message)
        if not match or len(match.group(1)) != count:
            raise RuntimeError('Truncated result chunk. Collection can be resumed.')
        encoded += match.group(1)
        partial.write_text(encoded)
        print(f'Collected {len(encoded)}/{metadata["length"]} encoded result bytes', flush=True)
    data = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(data).hexdigest().upper() != metadata['sha256'].upper():
        raise RuntimeError('Result archive SHA256 mismatch')
    destination = ROOT / 'results' / run_id
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                normalized = name.replace('\\', '/')
                if Path(normalized).is_absolute() or '..' in Path(normalized).parts:
                    raise RuntimeError('Unsafe result archive entry')
                if not name.endswith('/'):
                    target = destination / normalized
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
    print(f'Results collected: results/{run_id}/', flush=True)


def status(args):
    state = load_state()
    vm = az('vm', 'get-instance-view', *scoped(state), '-n', 'freezer-host')
    print(json.dumps({'stage': state['stage'], 'region': state['region'],
                      'size': vm['hardwareProfile']['vmSize'],
                      'status': [s['code'] for s in vm['instanceView']['statuses']],
                      'estimated_hourly_usd': state['estimate']['estimated_hourly_usd']}, indent=2))


def destroy(args):
    state = load_state()
    if args.confirm_resource_group != state['resource_group']:
        raise RuntimeError('Supply --confirm-resource-group with the exact dedicated lab group name')
    group = az('group', 'show', '--subscription', state['subscription'], '-n', state['resource_group'])
    if any((group.get('tags') or {}).get(k) != v for k, v in TAGS.items()):
        raise RuntimeError('Ownership tags do not match. Refusing deletion.')
    resources = az('resource', 'list', *scoped(state))
    if any(any((r.get('tags') or {}).get(k) != v for k, v in TAGS.items()) for r in resources):
        raise RuntimeError('Group contains resources without exact lab ownership tags. Refusing deletion.')
    az('group', 'delete', '--subscription', state['subscription'], '-n', state['resource_group'], '--yes')
    if az('group', 'exists', '--subscription', state['subscription'], '-n', state['resource_group']):
        raise RuntimeError('Resource group deletion not confirmed')
    STATE.unlink()
    print('Dedicated lab resource group deletion confirmed. Local results retained.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Read-only availability, quota, image and price checks')
    plan.add_argument('--subscription', required=True)
    plan.add_argument('--region', default='eastus')
    deploy_parser = commands.add_parser('deploy', help='Create a new isolated lab after explicit cost approval')
    deploy_parser.add_argument('--subscription', required=True)
    deploy_parser.add_argument('--region', default='eastus')
    deploy_parser.add_argument('--resource-group', default='freezer-lab')
    deploy_parser.add_argument('--approve-costs', action='store_true')
    deploy_parser.add_argument('--resume', action='store_true', help='Repair only the existing lab recorded in local state')
    deploy_parser.add_argument('--max-hourly-usd', type=float, default=0)
    configure_parser = commands.add_parser('configure', help='Transfer code and configure Hyper-V and guests')
    for child in (deploy_parser, configure_parser):
        child.add_argument('--interval-ms', type=int, default=500, choices=range(50, 10001), metavar='50..10000')
        child.add_argument('--check-timeout-ms', type=int, default=300, choices=range(50, 5001), metavar='50..5000')
        child.add_argument('--fall', type=int, default=2, choices=range(1, 11))
        child.add_argument('--rise', type=int, default=2, choices=range(1, 11))
    experiment = commands.add_parser('run', help='Run 2/5/7/10 second freezes in all three modes and collect results')
    experiment.add_argument('--repeats', type=int, default=1, choices=range(1, 101))
    commands.add_parser('collect', help='Download completed or partial results')
    commands.add_parser('status', help='Show compact status without environment identifiers')
    commands.add_parser('sync', help='Transfer current host code without changing infrastructure')
    remove = commands.add_parser('destroy', help='Delete only the exact tagged lab resource group')
    remove.add_argument('--confirm-resource-group', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            _, estimate = preflight(args.subscription, args.region)
            print(json.dumps(estimate, indent=2))
        elif args.command == 'sync':
            sync(load_state())
        else:
            globals()[args.command](args)
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
