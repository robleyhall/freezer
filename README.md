# Freezer: Azure nested Hyper-V freeze lab

> **Unsupported draft - lab use only.** This repository is experimental reference
> material for discussion and evaluation in isolated, non-production labs. It is
> not a supported product or a production-ready solution. Independently review
> and validate the code, security, costs, and workload behavior before use.
> Example configurations and HTTP test results do not establish support for
> other workloads or guarantee behavior during real Azure node maintenance.

## What this lets you test

**The intention is to simulate the application-visible effects of an Azure VM
`Freeze` event in a controlled lab.** Microsoft's Scheduled Events terminology
uses `Freeze` for a temporary VM pause: CPU execution and network connectivity
may be suspended while memory and open files remain intact. Such pauses can
occur during memory-preserving host maintenance or live migration. See
[Azure VM maintenance](https://learn.microsoft.com/en-us/azure/virtual-machines/maintenance-and-updates)
and [Scheduled Events: Freeze](https://learn.microsoft.com/en-us/azure/virtual-machines/windows/scheduled-events#event-properties).

We approximate that effect by **pausing an entire nested test VM for a few
seconds, then letting it continue**. This lets you observe its applications and
clients: do requests stall or fail, do clients time out or reconnect, and how
quickly does useful work resume? This is not an on-demand Azure platform
freeze, a live migration, or a claim of identical network and storage behavior.

An Azure VM acts as a Hyper-V node and hosts two smaller, nested VMs. We pause
one nested VM while the node and the other VM keep running. The pause comes
from outside the test VM, so its operating system and applications stop
together; it is not just stopping one application process.

The included example runs a simple web service on both nested VMs. HAProxy,
a software load balancer on the second VM, directs connections to either
service. We compare:

- **Direct access:** connect straight to the VM that will freeze.
- **React after failure:** let HAProxy notice the frozen VM and send new
  connections to the healthy VM.
- **Act on advance notice:** inject a test warning so HAProxy stops sending
  new connections to that VM before it freezes.

An existing connection stays attached to its original VM; the load balancer
cannot move it to the healthy one. That connection can still stall. You can
also test a different workload without a load balancer, but must install it
and adapt the client and measurements; the supplied results are HTTP-only.

**This tests the effect of one frozen VM, not a freeze of the shared node.**
It does not reproduce Azure maintenance or generate a real Azure freeze notice.

Operate from **WSL or Linux**. Azure CLI, Bicep, and Python control a Windows
Server Hyper-V host in Azure. PowerShell runs **only on that remote host**; you
do not need PowerShell on your workstation.

This is a fault-injection lab, not an implementation of Azure's host maintenance
system. Pausing a nested guest does **not** generate an Azure Scheduled Event.
See [methodology and limitations](docs/methodology.md) before interpreting results.

## Topology

```text
WSL/Linux: ./freezer -> Azure CLI / Azure Run Command
                         |
Azure: dedicated lab resource group, no existing network connections
  Windows Server 2022 host (Standard security, nested virtualization)
    Internal Hyper-V switch + Windows NAT: 192.168.250.0/24
      app1: .11:8000     Ubuntu HTTP service; pause target
      app2: .12:8000     Ubuntu HTTP service; never paused
            .12:8080     HAProxy, TCP forwarding + HTTP health checks
            .12:9999     HAProxy control, accessible only from parent host
    Parent host: traffic generator, freeze controller, event listener
```

Only the parent has an Azure NIC. The public IP provides explicit outbound
access for image/package downloads. The NSG denies **all inbound traffic**.
Management, code transfer, and result transfer use Azure Run Command. There
are no inbound SSH/RDP listeners opened by the deployment, no storage account,
no Bastion, and no public application endpoints.

## Prerequisites

- Linux/WSL with Python 3.10+, Bash, and Azure CLI signed in (`az login`).
- Bicep CLI available through `az bicep version` (install with `az bicep install`
  if missing). The VM template uses Compute API `2025-11-01` to request Standard
  security without subscription-wide feature registration.
- A **non-production subscription** with permissions to create a dedicated
  resource group, VM, disk, NIC, NSG, VNet, public IP, tags, and invoke VM Run
  Command. No Entra directory or data-plane storage roles are required.
- Quota and regional capacity for one supported Intel D-series Windows VM.

The operator supplies the subscription at runtime. Do not put identifiers,
credentials, account names, or organization details into source or reports.
Runtime state is stored in ignored `.local/` with restrictive permissions.

## Deploy and operate from Linux

```bash
# Read-only: check price, available SKU, family/total quota, and Windows image.
./freezer plan --subscription "$AZURE_SUBSCRIPTION_ID" --region eastus

# Review the estimate BEFORE approving. This refuses an existing resource group.
./freezer deploy --subscription "$AZURE_SUBSCRIPTION_ID" --region eastus \
  --resource-group freezer-lab --approve-costs --max-hourly-usd 0.25

# If deployment was interrupted, repair only the exact lab in local state.
./freezer deploy --resume --subscription "$AZURE_SUBSCRIPTION_ID" --region eastus \
  --resource-group freezer-lab --approve-costs --max-hourly-usd 0.25

./freezer status

# Run 12 cases: 2, 5, 7, 10 seconds x direct/reactive/event-drain.
./freezer run --repeats 1

# Resume downloading completed or partial artifacts, if needed.
./freezer collect

# Permanent deletion of this exact owned lab; local results are retained.
./freezer destroy --confirm-resource-group freezer-lab
```

Host setup installs Hyper-V, reboots once, downloads a SHA256-checked generic
Ubuntu 24.04 QCOW2 image, converts it to VHDX using `qemu-img`, creates two
guests with NoCloud seed disks, installs the
HTTP services and HAProxy, and verifies HTTP responses before marking the lab
ready. The Azure-specific Ubuntu VHD is **not** used: Canonical documents that
it is not suitable for ordinary on-premises/nested Hyper-V guests. The generic
`.tar.gz` contains a root filesystem, not the bootable partitioned disk; it is
not used either. QEMU is used only for offline image conversion, never as the
running hypervisor.

Deployment can take tens of minutes. Run Command transfers are deliberately
conservative: output is limited, so result archives are downloaded in small,
checksum-verified, resumable chunks. Collection can take longer than the tests.
Do not run two operators/commands against the same node concurrently.

## Installing a workload in a nested VM

The supplied workload is a small HTTP service. Installing a message broker or
another application is a separate step; the CLI does not install arbitrary
workloads. The general flow is:

1. **Check the guest requirements.** Verify the workload's supported OS,
   CPU, memory, disk, licensing, and network ports. The supplied guests run
   Ubuntu with 1 GiB RAM each, intended for the HTTP test, not a sizing promise
   for other workloads. Review capacity and costs before enlarging the node;
   a different guest OS also requires a different image/bootstrap.
2. **Reach the guest through the node.** From WSL/Linux, use Azure Run Command
   to run management steps on the Windows Hyper-V node. From that node, use
   SSH/SCP to reach the Linux guest over the internal switch. Bootstrap creates
   a lab SSH key on the node. The nested guest is not a separate Azure VM
   resource, so Azure Run Command does not address it directly. No public SSH
   or RDP endpoint is needed for this path.
3. **Install and configure inside the guest.** Stage the trusted installer or
   package, install its dependencies, create its service account and data
   directories, and configure its service, test data, and startup behavior.
   Keep credentials and licensed payloads out of source, seed templates, and
   reports; stage local private material only under ignored `.local/`.
4. **Make setup repeatable.** Record non-secret installation steps in a guest
   bootstrap or provisioning script. `lab/guest.yaml.in` is the current
   cloud-init template for new guests. Changing it does not update an existing
   guest, and rerunning `configure` does not reinstall its applications. Update
   that guest deliberately, or rebuild it only after protecting needed data.
5. **Verify the application before freezing it.** Confirm it starts, accepts
   a real client connection, and completes representative work. For a client
   elsewhere in the lab VNet, choose an access design from the next section.
   Configure only the required listening addresses, ports, and firewall rules.
6. **Measure that workload, not just HTTP.** Keep the workload under test in
   `freezer-app1`, the fixed pause target, and run its client outside that VM.
   The node calls `Suspend-VM` and `Resume-VM` against that guest. Adapt the
   traffic driver, health checks, and result collection for the actual protocol:
   record timeouts, reconnects, in-flight work, and recovery. The supplied
   `./freezer run` matrix still expects the HTTP services and HAProxy; it does
   not become a messaging test merely because another service is installed.

## Making a nested VM accessible from the lab VNet

**Current behavior:** the node has an Azure NIC in the lab VNet, but the nested
guests have only internal Hyper-V addresses. Windows NAT provides outbound
connectivity; it does not by itself publish a guest service to VNet clients.
The explicit NSG deny rule also blocks new inbound connections **from the VNet**,
not just from the internet. Neither access option below is implemented by the
current template or CLI; these are design steps, not completed network tests.

### Option 1: expose selected ports through the node

```text
Client in lab VNet -> node private IP:published port
                         -> nested guest IP:workload port
```

This is the smaller change when a workload needs only a few known ports. Add a
Windows NAT static port mapping or a suitable proxy on the node. Clients use
the node's private Azure address (or a private DNS name pointing to it), not the
guest address. Configure protocol-appropriate mappings and any additional
ports advertised by the application; do not assume one TCP port is sufficient.

Allow only the chosen lab client sources and ports through the Azure NSG and
the node and guest firewalls. The current blanket NSG deny is priority 100, so
adding a later allow will not work: the rules must be deliberately reprioritized
so narrow allows precede the blanket deny. Keep everything else denied. Bind
listeners to the private/internal interfaces, not the public endpoint. A
proxy or NAT mapping may change the source address seen by the application;
check that before relying on source-IP rules.

### Option 2: route directly to the guest's private address

```text
Client in lab VNet -> Azure route for the guest address range
                         -> node private Azure IP (router)
                           -> nested guest IP:workload port
```

This is a proposed routed design for clients that need to address guests
individually, based on Azure's virtual-appliance routing model:

1. Reserve a guest address range that does not overlap the Azure VNet or any
   other reachable network. Keep the node's Azure NIC private address stable.
2. On each lab client subnet that needs access, associate an Azure route table
   with a route for the guest range. Use next-hop type **Virtual appliance** and
   the node's private Azure NIC address as the next hop, not its Hyper-V address.
3. Enable Azure NIC IP forwarding on the node and routing/forwarding inside
   Windows between its Azure NIC and internal Hyper-V adapter. Enabling only
   the Azure setting is not sufficient.
4. Give guests the node's internal Hyper-V address as their gateway. Design
   the return path as well: replace or scope the current Windows NAT so it
   does not translate replies on the direct guest-to-client route. Internet
   egress, if needed, requires its own compatible NAT/egress design.
5. Allow the intended client-to-guest traffic in the applicable Azure NSGs
   and host/guest firewalls, including revising the current priority-100 deny
   as described above. Configure private DNS if clients need names rather
   than addresses. Verify connections and replies in both directions from
   an independent lab client, then repeat during a guest pause.

The guest still does not acquire its own Azure NIC or become a native Azure VM.
Simply assigning it an address from the Azure subnet or changing the Hyper-V
switch is not this routing setup. In both options, the node remains the network
path: pausing the guest leaves the node running; freezing the whole node could
interrupt every guest and its access path.

Any implementation must stay in the dedicated lab network, use the required
resource tags, and receive cost approval before adding Azure resources. Do not
connect this lab to existing or production networks. See the
[networking references](docs/references.md) for the documented building blocks;
the routed combination still needs end-to-end validation.

## Costs and isolation

The planner selects the cheapest **available and quota-eligible candidate in
its documented shortlist**, not an exhaustive global minimum. Candidates are
D2s_v4, D2ds_v4, D2s_v5, and D4s_v4; verify their nested virtualization support
when changing the shortlist. Spot is intentionally excluded so eviction cannot
confound freeze results.

As checked on 2026-11-19, the East US D2s_v4 Windows retail rate was $0.188/hour,
plus a $0.005/hour Standard public IPv4 address and $1.536/month for a 32 GiB
Standard HDD. Approximately **$0.1951/hour or $142.43 per 730-hour month** before
disk transactions, bandwidth, and tax. VM pricing is refreshed by `plan` and
`deploy`; disk/IP allowances must be rechecked against the pricing API when
reusing this estimate. Retail rates are not a promise about your invoice.

`--max-hourly-usd` is an **approval guard, not a spending cap**. Leaving the lab
running continues billing. Deallocating the VM stops compute charges but not
disk or public-IP charges; deleting the resource group stops retained-resource
charges. There is no automatic shutdown or deletion. Keep/cleanup is an
operator decision.

Every taggable resource uses `repo=freezer`, `project=freeze-lab`, and
`deployment=freezer-lab`. Azure does not propagate VM tags to its implicit OS
disk, so the deployer tags that disk immediately after creation and audits all
resource tags. Resource-group reuse and cleanup both check ownership.

## Experiment controls and outputs

The default health policy is `inter=500ms`, `timeout check=300ms`, `fall=2`,
`rise=2`. `lab/haproxy.cfg.in` contains the policy. Configure parameters are
available through `./freezer configure --help`. Existing guest configurations
are not overwritten by guest creation; see
[Installing a workload](#installing-a-workload-in-a-nested-vm) before changing an
existing guest.

Results are in ignored `results/<UTC-run-id>/`:

- Per case: `requests.csv`, `freeze.jsonl`, `health.jsonl`, `metadata.json`.
- Event-driven cases also contain `synthetic.json` and `notices.jsonl`.
- `summary.csv` and `report.md` compare failures, latency, detection,
  redirection, recovery, and 7-second deadline violations.

The generator keeps four TCP sessions established before a freeze and also
opens five new connections per second. It does not silently reconnect a failed
existing-session lane. Client/HAProxy timeouts intentionally exceed 7 seconds,
and retries are disabled, so the test exposes stalls instead of disguising
them as fast failures.

The listener can poll real IMDS or accept synthetic input, with separate modes.
Real events are never acknowledged automatically. Synthetic inputs are clearly
marked and are never posted to Azure. Recovery re-admits app1 only after an
HTTP probe succeeds. See [methodology](docs/methodology.md) for precise metrics.

## Validation

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q lab tools
bash -n freezer
az bicep build --file infra/main.bicep --stdout >/dev/null
```

`tests/Test-Freeze.ps1` uses mock Hyper-V/task commands to test guaranteed
resume attempts when pause or logging fails, and retention of the rescue task
when resume fails. Run with PowerShell on a test machine; it does not pause any
real VM. These tests are not a substitute for `./freezer run` on Azure.

The independent Windows Scheduled Task attempts resume 30 seconds after the
requested pause duration even if the controller process dies. It cannot
guarantee recovery while the parent host itself is stopped or broken.

## Scope

This version is a **single-node freeze lab** with two fixed Linux guests. A
general-purpose node/image/guest deployment service and routed Azure guest
network integration are possible extensions, not implemented CLI commands.
No customer-specific data belongs in this project. See the
[verified reference documentation](docs/references.md).
