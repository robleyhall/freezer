# Primary references

Checked 2026-11-19. Support, APIs, images, availability, and prices can change.
Recheck these before deploying a different size or reusing this lab later.

- [Microsoft Learn: Dsv4 sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv4-series)
  documents the 2-vCPU/8-GiB size and series feature support.
- [Microsoft Learn: nested virtualization setup](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/enable-nested-virtualization)
  documents Hyper-V setup, Standard security for Azure, and internal NAT.
- [Microsoft Learn: nested virtualization scope](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/nested-virtualization)
  explains support and performance-sensitive workload limitations. Nested Azure
  Local evaluation is not the same as a supported production Azure Local host.
- [Microsoft Learn: Suspend-VM](https://learn.microsoft.com/en-us/powershell/module/hyper-v/suspend-vm?view=windowsserver2025-ps)
  and [Resume-VM](https://learn.microsoft.com/en-us/powershell/module/hyper-v/resume-vm?view=windowsserver2025-ps)
  document pausing and resuming, distinct from saving or shutting down a guest.
- [Microsoft Learn: Trusted Launch FAQ](https://learn.microsoft.com/en-us/azure/virtual-machines/trusted-launch-faq)
  documents `securityType: Standard` with Compute API `2025-11-01` and later.
- [Microsoft Learn: Scheduled Events](https://learn.microsoft.com/en-us/azure/virtual-machines/windows/scheduled-events)
  specifies IMDS access, Freeze semantics, resource scope, event states, notice,
  and acknowledgment. Synthetic input in this lab is not an Azure event.
- [Microsoft Learn: Azure VM maintenance](https://learn.microsoft.com/en-us/azure/virtual-machines/maintenance-and-updates)
  describes VM pauses during memory-preserving host maintenance and live migration.
- [Microsoft Learn: Azure routing](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-networks-udr-overview)
  describes user-defined routes and virtual-appliance/IP-forwarding requirements.
- [Microsoft Learn: Azure NIC IP forwarding](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-network-network-interface#enable-or-disable-ip-forwarding)
  distinguishes Azure NIC forwarding from forwarding inside the guest OS.
- [Microsoft Learn: NAT static port mappings](https://learn.microsoft.com/en-us/powershell/module/netnat/add-netnatstaticmapping?view=windowsserver2025-ps)
  documents publishing an internal service through Windows NAT.
- [Microsoft Learn: NSG rule processing](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview)
  documents priority order and first-match behavior, including private VNet traffic.
- [Microsoft Learn: Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/windows/run-command)
  describes guest-agent execution and its output/operation limitations.
- [Microsoft Learn: retail prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
  is the public source used for planning. It contains no subscription discounts.
- [Canonical: cloud image artifacts](https://ubuntu.com/docs/public-images/public-images-reference/artifacts/)
  distinguishes generic images and Azure-specific VHDs, which must not be used
  as ordinary nested Hyper-V guest images.
- [Canonical: pinned Ubuntu release](https://cloud-images.ubuntu.com/releases/noble/release-20261017/)
  supplies the generic bootable QCOW2 `.img` and SHA256SUMS used by guest setup.
- [QEMU downloads](https://www.qemu.org/download/#windows) links the Windows
  binary distribution. Guest setup pins its 2026-08-11 installer and checks the
  published SHA512. Only `qemu-img` disk conversion is used; guests run in Hyper-V.
- [cloud-init: NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
  documents seed disks labeled `cidata`, user-data, metadata, and network config.
- [HAProxy: server runtime states](https://www.haproxy.com/documentation/haproxy-runtime-api/reference/set-server/)
  documents `ready`, `drain`, and `maint` behavior. Existing sessions are not
  migrated by a drain command.
- [HAProxy: configuration manual](https://docs.haproxy.org/2.8/configuration.html)
  describes TCP mode, HTTP checks, intervals, thresholds, retries, and timeouts.
- [Python Windows releases](https://www.python.org/downloads/windows/)
  supplies the embedded runtime; the host checks the executable signature.
