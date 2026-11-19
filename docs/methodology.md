# What this experiment does and does not establish

## Modes

1. **Direct:** traffic addresses app1 itself. There is no alternate destination.
2. **Reactive:** HAProxy uses HTTP health checks to stop assigning new TCP
   connections to app1 after its configured failure threshold.
3. **Event-drain:** a synthetic Freeze notice removes app1 from new connection
   selection before its pause. Existing TCP sessions remain attached to app1.

All three modes use the same service, traffic rate, pause implementation,
timeout budget, and observation windows. Each duration runs in a fixed order;
this introduces ordering bias. Use repeats and additional load profiles before
generalizing. This small host deliberately trades measurement isolation for
cost: both application guests, the proxy, and the controller share its CPU.

## Pause and rescue

The controller targets only a VM named `freezer-app1` whose Hyper-V notes match
`freezer-lab`. It rejects wildcard targets and non-running guests. It registers
an independent Scheduled Task before attempting the pause. The sequence is
`Suspend-VM`, confirm `Paused`, timestamp, wait, `Resume-VM`, confirm `Running`,
timestamp. A `finally` block attempts resume before logging or cleanup.

Recorded timestamps are UTC observations after state-confirming commands;
they do not expose Azure hardware pause instants. Windows scheduling and command
overhead mean the measured duration need not equal exactly 2, 5, 7, or 10 seconds.
The rescue task remains registered if resume fails. The controller also attempts
an independent resume before exiting its experiment cleanup.

## Traffic and metric definitions

HAProxy runs in TCP mode to preserve the relationship between an established
client connection and one backend. The backend returns a unique connection ID
and its generic name. Four persistent lanes are established before drain or
freeze, and at least one must be pinned to app1 or the experiment is invalid.
Each persistent lane is sequential: it waits for its outstanding request to
complete. A failure ends that lane; it is not quietly retried on app2.

New connections are independently scheduled at 5/s with enough worker capacity
for a 12-second stall. Scheduler lag is saved separately. An exhausted worker
pool invalidates the run instead of silently reducing offered traffic. The
service holds ordinary work requests for 50 ms, making in-flight freezes more
likely without introducing artificial multi-second processing delays.

`requests.csv` preserves baseline, freeze, and recovery traffic. Summary latency
and failure counts use **requests whose execution overlaps the confirmed
pause-to-resume window**, including requests started before the pause.

| Metric | Definition |
|---|---|
| Actual pause duration | Confirmed-resume UTC minus confirmed-pause UTC |
| Unhealthy detection | First sampled app1 `DOWN` after pause minus pause time; not applicable to direct mode |
| Redirection after removal | First successful NEW app2 response for a request initiated after observed `DOWN` or confirmed drain, minus that removal time |
| Notice processing | Confirmed drain minus notice received |
| Drain lead time | Pause minus confirmed drain; missing if drain was late |
| Failed request | HTTP non-200, transport error, invalid response, or request timeout |
| Deadline violation | Measured end-to-end request latency >= 7,000 ms, including failed requests |
| HTTP recovery | First observed successful app1 response after resume, even if its request started earlier |
| Backend re-admission | First sampled app1 `UP` after resume |

An absent observation is blank/null, never zero or an inferred success. A short
timeout that fails fast is **not** a successful request within the deadline.
P95 uses the nearest-rank method and includes failures. `summary.csv` also saves
total-window failures so problems outside the overlap window remain visible.
Redirection is an observed first successful alternate connection, not proof
that every connection was rerouted or that an existing TCP session migrated.

Health is sampled every 100 ms plus runtime-command and thread scheduling time.
This introduces uncertainty in detection/recovery metrics. The traffic source,
health monitor, and Hyper-V commands all observe the same Windows host clock;
Python request latency uses a monotonic clock. For stronger results move the
traffic generator and proxy to independent Azure VMs and capture packet traces.

## Scheduled Events

`lab/events.py` implements the IMDS Scheduled Events document shape, filters
Freeze events by status and mapped resource, deduplicates event IDs, and sends
HAProxy `set server apps/app1 state drain`. Runtime rejection is an error, and
failed drains are not marked as processed. Unknown resources cause no change.

There are separate live and synthetic modes. A synthetic notice is written
atomically to a local file; receipt and successful drain are logged before the
freeze begins. This tests advance-notice handling. It does not invoke Azure
maintenance, publish a platform event, or imitate Azure authentication.

A real parent-host Scheduled Event maps to **both** nested backends. Draining
both on this single-node lab leaves no independent healthy host. The listener
does not automatically acknowledge real events or automatically restore an
event-affected backend when an event disappears. A real operating service
would need explicit health checks and a recovery policy.

## Azure network integration

The implemented lab uses a Hyper-V internal switch and Windows NAT. Guests do
not have individual Azure NICs. The Azure NSG restricts the parent boundary;
guest firewalls restrict services on the internal network. Internal guest to
guest traffic does not traverse the parent Azure NIC as independent Azure VMs.

A future routed mode could allocate a non-overlapping guest prefix per node,
configure Azure user-defined routes via the parent NIC, and enable Azure NIC
and OS forwarding with explicit return routes. That is a separate architecture
change requiring end-to-end tests, per-guest firewall policy, DNS design, and
appropriate route/peering permissions. It is not enabled by this deployment.

## Transfer to stateful messaging

HTTP failover results do not prove AMQP, Kafka, database, or other stateful
session recovery. A load balancer cannot transfer protocol session state,
unsettled messages, transactions, locks, or leases to a different process.
A pause may outlast message locks or heartbeats while the original worker later
continues processing, leading to redelivery and duplicate side effects.

Validate the actual client reconnect policy, lock renewal, lease expiry,
acknowledgment/settlement timing, retry budget, idempotency, ordering, and poison
message handling. Test reconnect storms and in-flight work with an independent
healthy destination. Do not claim exactly-once processing or a production
7-second guarantee from this HTTP experiment.
