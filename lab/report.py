"""Generate CSV and Markdown from measured results; never invent missing samples."""
import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def json_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def truth(value):
    return str(value).lower() == 'true'


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def ms_between(end, start):
    return round(max(0, end - start) * 1000, 3) if end is not None and start is not None else None


def summarize(folder):
    metadata = json.loads((folder / 'metadata.json').read_text())
    events = {item['kind']: timestamp(item['utc']) for item in json_lines(folder / 'freeze.jsonl')}
    paused, resumed = events.get('paused'), events.get('resumed')
    if paused is None or resumed is None or resumed < paused:
        raise ValueError(f'{folder.name}: confirmed pause/resume timestamps are required')
    with (folder / 'requests.csv').open(newline='', encoding='utf-8-sig') as stream:
        requests = list(csv.DictReader(stream))
    health = [row for row in json_lines(folder / 'health.jsonl') if row['backend'] == 'app1']
    down = next((timestamp(row['utc']) for row in health
                 if timestamp(row['utc']) >= paused and row['status'].startswith('DOWN')), None)
    up = next((timestamp(row['utc']) for row in health
               if timestamp(row['utc']) >= resumed and row['status'] == 'UP'), None)
    notices = json_lines(folder / 'notices.jsonl')
    drained = next((timestamp(row['utc']) for row in notices if row['kind'] == 'drained'), None)
    notice = next((timestamp(row['utc']) for row in notices if row['kind'] == 'notice_received'), None)
    rows = []
    for lane in ('new', 'existing'):
        subset = [row for row in requests if (row['lane'] == 'new') == (lane == 'new')]
        exposure = [row for row in subset if timestamp(row['start_utc']) <= resumed
                    and timestamp(row['end_utc']) >= paused]
        if not exposure:
            raise ValueError(f'{folder.name}: no {lane} traffic overlaps the freeze')
        latencies = [float(row['latency_ms']) for row in exposure]
        failed = sum(not truth(row['ok']) for row in exposure)
        exceeded = sum(truth(row['deadline_exceeded']) for row in exposure)
        recovery = next((timestamp(row['end_utc']) for row in sorted(subset, key=lambda r: r['end_utc'])
                         if truth(row['ok']) and row['backend'] == 'app1' and timestamp(row['end_utc']) >= resumed), None)
        redirect_anchor = drained if metadata['mode'] == 'event-drain' else down
        redirected = next((timestamp(row['end_utc']) for row in sorted(subset, key=lambda r: r['end_utc'])
                           if redirect_anchor is not None and timestamp(row['start_utc']) >= redirect_anchor
                           and truth(row['ok']) and row['backend'] == 'app2'), None)
        rows.append(dict(experiment=metadata['experiment'], mode=metadata['mode'],
                         duration_s=metadata['duration_s'], lane=lane, validation=metadata['validation'],
                         paused_utc=datetime.fromtimestamp(paused, timezone.utc).isoformat(),
                         resumed_utc=datetime.fromtimestamp(resumed, timezone.utc).isoformat(),
                         actual_pause_ms=ms_between(resumed, paused),
                         exposure_requests=len(exposure), failed_requests=failed, deadline_exceeded=exceeded,
                         max_latency_ms=max(latencies), p95_latency_ms=percentile(latencies, .95),
                         detect_unhealthy_ms=ms_between(down, paused) if metadata['mode'] != 'direct' else None,
                         redirect_after_removal_ms=ms_between(redirected, redirect_anchor) if lane == 'new' else None,
                         notice_to_drain_ms=ms_between(drained, notice),
                         drain_lead_ms=ms_between(paused, drained) if drained and paused >= drained else None,
                         backend_up_after_resume_ms=ms_between(up, resumed),
                         first_app1_response_after_resume_ms=ms_between(recovery, resumed),
                         max_scheduler_lag_ms=max(float(r['scheduler_lag_ms']) for r in subset),
                         requests_total=len(subset), failures_total=sum(not truth(r['ok']) for r in subset),
                         pass_observed=failed == 0 and exceeded == 0))
    return rows, metadata


def generate(root):
    root = Path(root)
    rows, metadata, incomplete = [], [], []
    folders = sorted(path for path in root.iterdir() if path.is_dir())
    for folder in folders:
        if not (folder / 'metadata.json').exists():
            incomplete.append(folder.name)
            continue
        summary, info = summarize(folder)
        rows.extend(summary)
        metadata.append(info)
    if not rows:
        raise ValueError('No completed experiments with confirmed measurements')
    with (root / 'summary.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    expected = {(mode, duration) for mode in ('direct', 'reactive', 'event-drain') for duration in (2, 5, 7, 10)}
    missing = expected - {(m['mode'], m['duration_s']) for m in metadata}
    validated = not missing and not incomplete and all(m['validation'] == 'azure-hyper-v' for m in metadata)
    lines = ['# Freeze lab measured results', '',
             f'Validation: **{"Azure nested Hyper-V matrix complete" if validated else "INCOMPLETE / NOT A FULL AZURE VALIDATION"}**.', '',
             'Only requests overlapping the confirmed pause/resume window are counted in the table. '
             'A pass requires both HTTP success and latency strictly below 7 seconds. '
             'Fast errors are failures, not successful deadline protection.', '',
             '| Mode | Freeze (s) | TCP lane | Requests | Failures | >=7 s | Max ms | p95 ms | Observed |',
             '|---|---:|---|---:|---:|---:|---:|---:|---|']
    for row in rows:
        lines.append(f'| {row["mode"]} | {row["duration_s"]} | {row["lane"]} | {row["exposure_requests"]} | '
                     f'{row["failed_requests"]} | {row["deadline_exceeded"]} | {row["max_latency_ms"]:.1f} | '
                     f'{row["p95_latency_ms"]:.1f} | {"PASS" if row["pass_observed"] else "FAIL"} |')
    lines += ['', '## Routing and recovery', '',
              '| Mode | Freeze (s) | Detect unhealthy ms | Redirect after removal ms | Notice to drain ms | Backend UP after resume ms |',
              '|---|---:|---:|---:|---:|---:|']
    for row in rows:
        if row['lane'] == 'new':
            def value(key):
                return 'not observed / not applicable' if row[key] is None else f'{row[key]:.1f}'
            lines.append(f'| {row["mode"]} | {row["duration_s"]} | {value("detect_unhealthy_ms")} | '
                         f'{value("redirect_after_removal_ms")} | {value("notice_to_drain_ms")} | '
                         f'{value("backend_up_after_resume_ms")} |')
    lines += ['', '## Interpretation and limits', '',
              '- Detection is the first sampled HAProxy DOWN state after confirmed pause. Sampling is every 100 ms plus command time; missing DOWN is not zero.',
              '- Redirection is the first successful NEW connection to app2 initiated after observed DOWN (reactive) or confirmed drain (event-driven). It is not a guarantee about every connection or retry.',
              '- HTTP recovery is the first observed app1 response after resume; it can complete a request started before the freeze. Backend UP measures HAProxy re-admission separately.',
              '- Existing TCP lanes are established before drain/freeze. HAProxy uses TCP mode, so these sessions stay pinned; a failed lane stops instead of quietly reconnecting.',
              '- Timeouts are 12 seconds at the client and 15 seconds at HAProxy, intentionally above the 7-second deadline. Retries are disabled. The lab exposes stalls rather than hiding them with a short timeout.',
              '- Synthetic events are injected before pausing. Suspend-VM does NOT generate an Azure Scheduled Event. The listener never acknowledges or fabricates events to Azure IMDS.',
              '- Both guests, HAProxy, and the traffic controller share ONE Azure host VM. Only app1 is paused. A real parent/physical-host freeze could stop every component; this topology cannot prove host-level resilience.',
              '- HAProxy shares app2 to minimize cost. CPU scheduling, NAT, low-cost HDD, and guest pause semantics differ from Azure maintenance. UTC timestamps bracket completed Hyper-V commands, not exact hardware freeze instants.',
              '- Stateful messaging is not validated: AMQP/TCP sessions do not migrate to another backend. Locks/leases can expire; reconnects, redelivery, duplicates, ordering, settlement, and in-flight work require protocol-specific tests and idempotent handlers.',
              '- These are finite observations at 5 new connections/second plus 4 persistent lanes, not a production SLA or statistical proof. Repeat under realistic load, with an independent traffic source and proxy on separate Azure hosts.',
              '- Raw CSV contains all requests including baseline/recovery and scheduler lag. A large scheduling lag means the traffic source itself was delayed and the run should not be treated as a latency guarantee.', '']
    if missing or incomplete:
        lines += ['Incomplete cases: ' + ', '.join(sorted(incomplete) + [f'{m}/{d}s' for m, d in sorted(missing)]), '']
    (root / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('results', type=Path)
    generate(parser.parse_args().results)
