# Monitor the hosted pilot (R10)

This page sets up the R10 gate of the [hosted rollout](hosted-rollout#r10-monitoring):
alerts for the public gateway, the Controller and each deployment's migration, using
the existing Prometheus and Grafana. It is opt-in and changes nothing until an
operator installs it. Nothing here deploys a service, edits the vLLM job or its file
discovery, changes the default contact point, or turns on H09.

**Status: repository implementation only.** The R10 live checks at the
[end of this page](#live-acceptance-r10) are **PENDING** until the operator records
evidence. A merged pull request or green CI does not complete R10.

## How it fits together

```text
Docker host                                    monitoring stack (separately owned)
┌──────────────────────────────────────────┐   ┌──────────────────────────────────┐
│ systemd timers                           │   │ node_exporter                    │
│  nest-monitor.timer (every minute)       │   │  --collector.textfile.directory  │
│   docker ps / inspect / logs (read-only) │   │  ← read-only mount of the dir    │
│  nest-monitor-probe.timer (daily)        │   │ Prometheus job `nest`            │
│   verify-public-grpc.py, no credential   │   │ Grafana-managed rules → one      │
│  → /var/lib/node_exporter/textfile/nest  │──▶│   named contact point            │
└──────────────────────────────────────────┘   └──────────────────────────────────┘
```

`scripts/nest-monitor.py` turns each signal into a few bounded counters and gauges
and writes them as Prometheus text files. node_exporter, which already runs, serves
them. No exporter is added, no port is opened, Envoy's admin interface stays on its
loopback address inside the gateway container, and no container gets the Docker
socket.

| Signal | Collector | Series |
| --- | --- | --- |
| Certificate days left, public probe | `probe` (daily) | `nest_public_probe_*`, `nest_public_tls_certificate_days_left` |
| Envoy requests, `RESOURCE_EXHAUSTED`, `UH`/`UF` | `gateway` | `nest_gateway_*` |
| Controller `RPC refused` / `RPC rate-limited` | `controller` | `nest_controller_*` |
| Migration status after a deploy | `migration` | `nest_migration_*` |
| Each collector's own health | all | `nest_monitor_*{collector=...}` |

## Public probe

`nest-monitor.py probe` runs `scripts/verify-public-grpc.py` against the gateway,
without credentials: TLS and the certificate for the name, wrong name refused,
HTTP/1.1-only refused, `UNAUTHENTICATED` without a session, the health path refused,
and each `--origin` unreachable. It refuses `--signed-in` and `--execute`, and removes
every `NEST_` setting from the verifier's environment, so no session is read and no
job is submitted. It requires `--ca-file` (the staging CA that issued the gateway's
certificate, so a certificate from another CA fails) or `--system-roots` (a
production certificate from a public CA).

It writes `nest_public_probe_success` (1 when every check passed), one
`nest_public_probe_check_success{check=...}` per check (`origin_unreachable_<address>`
becomes `origin_unreachable`: no address is a label), the certificate's
`nest_public_tls_certificate_days_left`, and the times of the last run and the last
pass. A run that fails keeps the time of the last pass, so a run of failures shows
as stale as well as failed.

## Gateway

Envoy's stats would need its admin port, which listens only on `127.0.0.1` inside
the gateway container, and they do not say who answered `RESOURCE_EXHAUSTED`. The
access log does: the template writes one line per request with the path, gRPC
status, HTTP status and response flags, and never a header or body.

`nest-monitor.py gateway` reads the gateway's new log lines (`docker logs --since`,
from where the previous run stopped) and counts:

- `nest_gateway_requests_total{route, code}`: `route` is `service` for
  `/nest.ExecutionService/*` and `other` for anything else; `code` is one of the 17
  gRPC codes, `UNSET` or `OTHER`. The method and the rest of the path are dropped.
- `nest_gateway_resource_exhausted_total{source}`: `gateway` when Envoy's own rate
  limit answered (flag `RL`), `controller` when the Controller's answer was passed
  through.
- `nest_gateway_response_flags_total{flag}`: `UH`, `UF`, `UO`, `URX`, `UT`, `UC`,
  `UR`, `LR`, `DC`, `DT`, `SI`, `RL`, `NR`, `DPE`, `UPE`, `OM`, else `other`.
- `nest_gateway_access_log_unparsed_total`: lines that look like access-log lines
  but do not match, so a format change cannot silently stop the counts.

Connections Envoy closes before HTTP (RBAC on the edge address, another SNI name, the
connection limit) write no access-log line and are not counted. The first run starts
at the current time instead of replaying the log; a recreated gateway container is
read from its start. Counters live in the state directory and only grow.

## Controller

`nest-monitor.py controller` counts the Controller's warnings, matched only at the
start of a line in the Controller's log format and only from the `nest.rpc_guard`
logger, so text inside another message cannot pose as one:

- `nest_controller_rpc_refused_total{reason="origin"}`: `RPC refused: connection
  from outside NEST_GRPC_ALLOWED_PEERS`.
- `nest_controller_rpc_rate_limited_total{scope}`: `RPC rate-limited scope=user` or
  `scope=address`.

The `client=` address, the request ID and everything else on the line are dropped.
This needs no log store; Loki is not assumed.

## Migration status

`nest-monitor.py migration` classifies the current deployment's migration, using
`nest-deploy-doctor.py`'s own classification and only `docker ps` and
`docker container inspect`:

| Status | Meaning |
| --- | --- |
| `completed` | the newest `migrate` container exited 0 for this deploy, or that was recorded for the current Controller container |
| `failed` | it exited non-zero |
| `stuck` | a `migrate` container never started (see [recovery](deployment-recovery)) |
| `in_progress` | running, waiting for PostgreSQL, or just created |
| `stale` | the newest `migrate` container last finished (or started) over 10 minutes before the Controller container was created: it belongs to an earlier deploy. A deploy that recreates only the Controller starts the existing `migrate` container again; it then counts by when it ran, not when it was created |
| `absent` | no `migrate` container, and no completion recorded for this Controller container |
| `unknown` | Docker could not be queried, or there is no Controller container |

**Completion evidence.** A `migrate` container is one-shot, and it can disappear
after it finished (a cleanup, a prune, a deploy that drops the `migrate` profile).
Each time the collector sees a completed, current one, it records the `migrate`
container, its finish time and the Controller container's ID. While that Controller
container is the running one (restarts keep its ID), the recorded evidence keeps the
status `completed`, with `nest_migration_evidence_recorded 1`. A new deploy recreates
the Controller, so the old evidence stops counting, and the status is `absent` or
`stale` until the new migration is seen. So that evidence is captured, keep the
collector timer running, and right after each deploy run:

```bash
python3 scripts/nest-deploy-doctor.py wait-migrate --project <appName> --timeout 300
sudo systemctl start nest-monitor.service
```

`absent` right after a deploy usually means `COMPOSE_PROFILES` has no `migrate`: the
deploy ran no migration at all. A current schema is a separate question: keep
checking it read-only with `nest-db verify` as in the
[Dokploy guide](dokploy). The collector never upgrades, recovers, restarts or
removes anything.

`nest_migration_deploy_timestamp_seconds` is when the current Controller container
was created; the alert waits 15 minutes after it.

## Collectors

Every collector writes `nest_monitor_last_run_timestamp_seconds`,
`nest_monitor_last_success_timestamp_seconds` and `nest_monitor_collector_success`
with its `collector` label. A collector fails (exit 1, success 0, last success
unchanged) when it cannot do its job: the container it reads is missing or not
running, Docker does not answer, or the verifier cannot run. Its counters are kept.
A migration status other than `completed`, or a failing probe, is a result, not a
collector failure.

Nothing written holds a log line, a header, a token, a client address, or a request,
User, Project or execution ID: every label value comes from the fixed lists above
and is checked again before it is written. The terminal output is one summary line
per collector.

## Alerts

The rules are defined once in `deploy/monitoring/nest_alerts.py`, which renders
`deploy/monitoring/prometheus/nest-alerts.rules.yml` (checked and unit-tested with
`promtool` in CI) and the Grafana provisioning file. Each fires only on its own
`nest_*` series.

| Alert | Fires when | For | Severity |
| --- | --- | --- | --- |
| `NestPublicProbeFailed` | the last probe failed any check | 1m | critical |
| `NestPublicProbeStale` | no passing probe for 26 h | 10m | warning |
| `NestCertificateExpiringSoon` | `days_left` under 14 | 1m | warning |
| `NestGatewayResourceExhaustedHigh` | over 20% of requests in 15 min were `RESOURCE_EXHAUSTED`, and there were at least 20 | 10m | warning |
| `NestGatewayUpstreamFailures` | at least 3 `UH` or `UF` lines in 15 min | 5m | critical |
| `NestGatewayAccessLogUnparsed` | any unparsed access-log line in 1 h | 0m | warning |
| `NestControllerRpcRefused` | any `RPC refused` in 15 min | 0m | warning |
| `NestControllerRateLimitedSustained` | at least 10 `RPC rate-limited` in 15 min | 10m | warning |
| `NestMigrationNotCompleted` | the status is not `completed` 15 min after the Controller container was created | 5m | critical |
| `NestMonitorCollectorFailing` | a gateway, controller or migration collector has not succeeded for 15 min | 5m | warning |
| `NestMonitorCollectorMissing` | fewer than the 4 collectors' series are scraped | 30m | warning |

Why these bounds:

- **Rate window and floor.** The pilot's quotas are expected to reject some calls.
  One rejection, or many among a handful of requests, does not alert: the ratio
  needs at least 20 requests in the 15-minute window and must hold for 10 minutes.
  `nest_gateway_resource_exhausted_total{source}` then says whether the gateway's
  overall limit or the Controller's quotas answered.
- **No data.** With no traffic the ratio is undefined and does not alert. Missing
  series are not healthy: if a collector stops, `NestMonitorCollectorFailing` or
  `NestMonitorCollectorMissing` fires, and a probe that stops passing makes
  `NestPublicProbeStale` fire after 26 hours. A certificate whose days left cannot be
  read is a failed probe.
- **`RPC refused`** should never happen once the gateway is the only path, so one is
  enough. Rate-limit warnings alert only when sustained.

Notifications carry the fixed summary and description of each rule and a link to
this page: no templated value, label from a log, or address.

## Privileges

| Unit | User | Needs |
| --- | --- | --- |
| `nest-monitor.service` | `nest-monitor`, groups `docker`, `nest-textfile` | `docker ps`, `docker container inspect`, `docker logs` |
| `nest-monitor-probe.service` | `nest-monitor-probe`, group `nest-textfile` | outbound HTTPS to the gateway; no Docker |

Membership of `docker` is equivalent to root on the host: the collector runs only the
three read-only commands above (the tests fail if it runs any other), but the group
itself does not enforce that. If that is not acceptable, the alternatives are:

- a root-owned wrapper that runs exactly those commands for this project, allowed by
  one `sudoers` line for `nest-monitor`;
- running only the probe (no Docker at all) and accepting that the gateway, Controller
  and migration signals are checked by hand with `nest-deploy-doctor.py`;
- a log shipper that already runs on the host, if one is approved later; Loki is
  disabled today.

Do not give an exporter container the Docker socket, publish Envoy's admin port, or
attach the monitoring stack to the gateway's networks to collect these signals.

## Install the collectors

From a separate, read-only checkout of the reviewed commit, not the Dokploy checkout:

```bash
sudo git clone https://github.com/cerulean-works/nest.git /opt/nest-monitor
sudo git -C /opt/nest-monitor checkout <reviewed commit>
sudo useradd --system --no-create-home nest-monitor
sudo useradd --system --no-create-home nest-monitor-probe
sudo groupadd --system nest-textfile
sudo usermod -aG docker,nest-textfile nest-monitor
sudo usermod -aG nest-textfile nest-monitor-probe
sudo install -d -m 2775 -g nest-textfile /var/lib/node_exporter/textfile/nest
sudo install -d /etc/nest-monitor
sudo install -m 0644 /opt/nest-monitor/deploy/monitoring/systemd/nest-monitor.env.example \
    /etc/nest-monitor/nest-monitor.env   # then edit: project, address
sudo install -m 0644 <staging CA PEM> /etc/nest-monitor/staging-ca.pem
sudo cp /opt/nest-monitor/deploy/monitoring/systemd/nest-monitor*.{service,timer} /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/nest-monitor*.service
sudo systemctl daemon-reload
```

The probe runs the verifier with `uv` (`/usr/local/bin/uv` in the unit; adjust the
path). Its virtual environment and cache are under `/var/cache/nest-monitor-probe`,
so the checkout stays unchanged.

Run each once by hand and read what it would publish before anything scrapes it:

```bash
sudo systemctl start nest-monitor.service nest-monitor-probe.service
journalctl -u nest-monitor.service -u nest-monitor-probe.service -n 20
cat /var/lib/node_exporter/textfile/nest/*.prom
```

Expected: `migration: completed` (or the status you know to be true), probe `pass`,
and `nest_public_tls_certificate_days_left` matching R4. Then enable the timers:

```bash
sudo systemctl enable --now nest-monitor.timer nest-monitor-probe.timer
```

## Expose the text files

This is a change to the separately owned monitoring stack: open it there as a
reviewed proposal and apply it through its own deploy, not by editing its live
checkout.

1. node_exporter: add `--collector.textfile.directory=/textfile` and a read-only
   bind mount `/var/lib/node_exporter/textfile/nest:/textfile:ro`. If node_exporter
   already reads a textfile directory, point the units' `--textfile-dir` there
   instead.
2. Prometheus: add the separate `nest` job of
   `deploy/monitoring/prometheus/nest-scrape.yml.example`. It asks node_exporter for
   its textfile collector only and keeps only `nest_*` and the textfile collector's
   error and mtime series. Check the result with `promtool check config` before the
   reload. Once the directory is enabled, the existing node_exporter job serves the
   same `nest_*` series too; every rule and dashboard query selects `job="nest"`, so
   that copy is never counted. If the job gets another name, render the rules with
   `--job <name>`. Optionally drop the copy from the node_exporter job with
   `metric_relabel_configs: [{source_labels: [__name__], regex: "nest_.*", action: drop}]`.
3. In Prometheus, check that the series arrive
   (`nest_monitor_collector_success{job="nest"}`: four collectors, all 1) and that
   `node_textfile_scrape_error` is 0.

## Provision the alerts

1. The operator chooses and creates the contact point (the approved channel and its
   credentials stay in Grafana; none is in this repository). Use Grafana's **Test**
   button on it once.
2. Render the provisioning file with the Prometheus data source UID (Connections →
   Data sources → the Prometheus one; the UID is in its URL) and that contact point,
   paused for the first rollout:

   ```bash
   python3 deploy/monitoring/nest_alerts.py grafana \
       --datasource-uid <uid> --contact-point <contact point> --paused > nest-alerts.json
   ```

3. Add it to Grafana's alerting provisioning directory through the monitoring stack's
   reviewed change, and restart or reload Grafana. The rules appear in a `Nest`
   folder, each sending only to that contact point; the notification policy tree and
   the default contact point are unchanged.
4. With the rules paused, open each rule's query and check that it answers 0. Then
   render again without `--paused` and deploy that.

   On a deployment whose `migrate` container is already gone (the staging baseline
   of #114), the migration status is `absent` and `NestMigrationNotCompleted`'s
   query answers 1: that is correct, since no completion was ever observed for the
   running Controller container. Unpause every other rule, and keep only that one
   paused (edit the rendered file's `isPaused` for its UID, `nest-migration-not-completed`)
   until the next approved deploy, after which `migrate-status` and the collector
   show `completed`; then unpause it and tick the migration item under
   [Live acceptance](#live-acceptance-r10).

Prometheus can evaluate the same rules instead (`rule_files:` with
`nest-alerts.rules.yml`), but Prometheus has no Alertmanager here, so Grafana-managed
rules are the delivery path.

Every rule's query in Grafana is `count(<expression>) or vector(0)` with the
condition "above 0", so a healthy deployment answers 0, not "no data". No data
(the data source itself failing) alerts.

### Dashboard queries

Every query selects `job="nest"`: the existing node_exporter job serves the same
series from the textfile directory, and counting both doubles them.

| Panel | Query |
| --- | --- |
| Certificate days left | `nest_public_tls_certificate_days_left{job="nest"}` |
| Hours since a passing probe | `(time() - nest_public_probe_last_success_timestamp_seconds{job="nest"}) / 3600` |
| Requests per minute by code | `sum by (code) (rate(nest_gateway_requests_total{job="nest"}[5m])) * 60` |
| `RESOURCE_EXHAUSTED` share | `sum(increase(nest_gateway_resource_exhausted_total{job="nest"}[15m])) / sum(increase(nest_gateway_requests_total{job="nest"}[15m]))` |
| Rejections by source | `sum by (source) (increase(nest_gateway_resource_exhausted_total{job="nest"}[15m]))` |
| Response flags | `sum by (flag) (increase(nest_gateway_response_flags_total{job="nest"}[15m]))` |
| Controller warnings | `sum(increase(nest_controller_rpc_refused_total{job="nest"}[15m]))`, `sum by (scope) (increase(nest_controller_rpc_rate_limited_total{job="nest"}[15m]))` |
| Migration status | `nest_migration_status{job="nest"} == 1` |
| Collector health | `nest_monitor_collector_success{job="nest"}`, `time() - nest_monitor_last_success_timestamp_seconds{job="nest"}` |

## Test the alerts safely

None of these generates load, signs in, submits a job or changes a route:

- **Probe failure and recovery.** Run the probe once with a CA that did not issue the
  certificate, for example a freshly generated one, then once with the right one:

  ```bash
  openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=drill -days 1 \
      -keyout /tmp/drill.key -out /tmp/drill-ca.pem
  sudo -u nest-monitor-probe python3 /opt/nest-monitor/scripts/nest-monitor.py probe \
      <gateway>:443 --ca-file /tmp/drill-ca.pem \
      --runner "/usr/local/bin/uv run --frozen python" \
      --textfile-dir /var/lib/node_exporter/textfile/nest --state-dir /var/lib/nest-monitor-probe
  # NestPublicProbeFailed fires and notifies; then:
  sudo systemctl start nest-monitor-probe.service   # passes; the alert resolves
  ```

- **Collector failure.** `sudo systemctl stop nest-monitor.timer` for 25 minutes:
  `NestMonitorCollectorFailing` fires. Start the timer again; it resolves.
- **Certificate and migration thresholds** are exercised by `promtool` against
  synthetic series in CI (`tests/test_monitoring.py`), including the boundaries
  (14 days does not fire, 13 does; the 15-minute grace), so no real expiry or failed
  migration is needed.

Record the notification received and the time it resolved for each drill.

## Roll back

Each step is independent and leaves the deployment untouched:

1. `sudo systemctl disable --now nest-monitor.timer nest-monitor-probe.timer`, then
   remove `/var/lib/node_exporter/textfile/nest/nest_*.prom`. The series disappear
   (and `NestMonitorCollectorMissing` fires if the rules remain).
2. Remove the Grafana provisioning file through the monitoring stack's change, and
   delete the `Nest` folder's rules (provisioned rules are not removed just by
   deleting the file; add a `deleteRules` entry for each UID, or delete the folder).
3. Remove the `nest` scrape job and the textfile mount.

## Live acceptance (R10)

**PENDING** until the operator records each item in the rollout issue:

- [ ] Collectors installed from a reviewed commit; first manual run output reviewed
      (no address, token or ID in any `.prom` file).
- [ ] Monitoring-stack change reviewed by its owner: textfile mount, `nest` job,
      `node_textfile_scrape_error` 0, four collectors reporting 1.
- [ ] Probe series match the R4 verifier output (`days_left`, every check passing).
- [ ] Gateway and Controller counters move with the normal R4/R9 traffic already
      planned (no extra load).
- [ ] Migration status `completed` after the next approved deploy, and the recorded
      evidence survives the `migrate` container's removal, if it is removed.
- [ ] Contact point approved by the operator, its **Test** delivered, rules
      provisioned paused, queries answering 0, then unpaused.
- [ ] Probe-failure and collector-failure drills: notification received and
      resolved.
