#!/usr/bin/env python3
"""
The Nest hosted-pilot alert rules (R10), defined once and rendered two ways.

    python3 deploy/monitoring/nest_alerts.py prometheus > nest-alerts.rules.yml
    python3 deploy/monitoring/nest_alerts.py grafana \\
        --datasource-uid UID --contact-point NAME > nest-alerts.json

`prometheus` prints a Prometheus rule file. The repository's copy,
deploy/monitoring/prometheus/nest-alerts.rules.yml, is checked and unit-tested with
`promtool` (nest-alerts.test.yml beside it) against synthetic series.

`grafana` prints a Grafana alerting provisioning file of Grafana-managed rules in a
`Nest` folder, querying the Prometheus data source `--datasource-uid`, each routed
straight to the existing contact point `--contact-point` (a rule's notification
settings): it neither defines nor replaces a notification policy or contact point,
so the default contact point and every other route stay as they are. `--paused`
provisions every rule paused, to check the queries before anything can notify. Each Grafana
rule's query is the Prometheus expression wrapped as `count(EXPR) or vector(0)`, so
a healthy deployment answers 0 (not "no data") and the condition is "above 0".

Every expression reads only the `nest_*` series of scripts/nest-monitor.py, so the
rules hold no address, token or ID and match no other job. Annotations are fixed
text without templates: a notification carries the rule name, never a log line.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

GROUP = "nest-hosted-pilot"
FOLDER = "Nest"
INTERVAL = "1m"
RUNBOOK = "https://github.com/cerulean-works/nest/blob/main/docs/source/monitoring.md"

# Thresholds (docs/source/monitoring.md#alerts explains each choice).
CERTIFICATE_DAYS = 14
PROBE_STALE_SECONDS = 26 * 3600  # a daily probe, with two hours of slack
COLLECTOR_STALE_SECONDS = 900  # collect runs every minute
RATE_WINDOW = "15m"
EXHAUSTED_RATIO = 0.2
MIN_REQUESTS = 20  # no ratio alert below this many requests in the window
UPSTREAM_FAILURES = 3
RATE_LIMITED_WARNINGS = 10
MIGRATION_GRACE_SECONDS = 900

LOG_COLLECTORS = "gateway|controller|migration"
ALL_COLLECTORS = "probe|gateway|controller|migration"


@dataclass(frozen=True)
class Rule:
    name: str
    expr: str
    duration: str
    severity: str
    summary: str
    description: str
    anchor: str

    @property
    def uid(self) -> str:
        return "nest-" + re.sub(r"(?<!^)(?=[A-Z])", "-", self.name.removeprefix("Nest")).lower()


RULES = (
    Rule(
        "NestPublicProbeFailed",
        "max(nest_public_probe_success) == 0",
        "1m",
        "critical",
        "The daily public gRPC probe failed",
        "The credential-free verify-public-grpc.py run failed at least one check "
        "(TLS, name, ALPN, unauthenticated refusal, path refusal or origin exposure).",
        "public-probe",
    ),
    Rule(
        "NestPublicProbeStale",
        f"time() - max(nest_public_probe_last_success_timestamp_seconds) > {PROBE_STALE_SECONDS}",
        "10m",
        "warning",
        "The public gRPC probe has not passed for over 26 hours",
        "No passing credential-free probe in 26 hours: the daily timer is not running, "
        "or every run since has failed.",
        "public-probe",
    ),
    Rule(
        "NestCertificateExpiringSoon",
        f"min(nest_public_tls_certificate_days_left) < {CERTIFICATE_DAYS}",
        "1m",
        "warning",
        "The public gRPC certificate expires in under 14 days",
        "Renewal normally happens 30 days before expiry; check the grpc-certificates "
        "container and its last renewal.",
        "public-probe",
    ),
    Rule(
        "NestGatewayResourceExhaustedHigh",
        f"(sum(increase(nest_gateway_resource_exhausted_total[{RATE_WINDOW}])) / "
        f"sum(increase(nest_gateway_requests_total[{RATE_WINDOW}])) > {EXHAUSTED_RATIO}) "
        f"and on() (sum(increase(nest_gateway_requests_total[{RATE_WINDOW}])) >= {MIN_REQUESTS})",
        "10m",
        "warning",
        "Over 20% of gateway requests are RESOURCE_EXHAUSTED",
        "Sustained for 10 minutes over at least 20 requests in 15 minutes. "
        "nest_gateway_resource_exhausted_total tells the gateway's rate limit from the "
        "Controller's quotas.",
        "gateway",
    ),
    Rule(
        "NestGatewayUpstreamFailures",
        f'sum(increase(nest_gateway_response_flags_total{{flag=~"UH|UF"}}[{RATE_WINDOW}])) '
        f">= {UPSTREAM_FAILURES}",
        "5m",
        "critical",
        "The gateway cannot reach the Controller (UH/UF)",
        "Envoy logged at least 3 requests with no healthy upstream or an upstream "
        "connection failure in 15 minutes; run nest-deploy-doctor.py gateway-runtime.",
        "gateway",
    ),
    Rule(
        "NestGatewayAccessLogUnparsed",
        "sum(increase(nest_gateway_access_log_unparsed_total[1h])) > 0",
        "0m",
        "warning",
        "The gateway access log no longer matches the monitor's format",
        "Gateway counters may be incomplete: the Envoy access-log format changed "
        "without scripts/nest-monitor.py.",
        "gateway",
    ),
    Rule(
        "NestControllerRpcRefused",
        f"sum(increase(nest_controller_rpc_refused_total[{RATE_WINDOW}])) >= 1",
        "0m",
        "warning",
        "The Controller refused calls from outside its allowed peers",
        "A call reached the Controller from an address outside NEST_GRPC_ALLOWED_PEERS: "
        "a network path that bypasses the gateway, or a changed gateway address.",
        "controller",
    ),
    Rule(
        "NestControllerRateLimitedSustained",
        f"sum(increase(nest_controller_rpc_rate_limited_total[{RATE_WINDOW}])) "
        f">= {RATE_LIMITED_WARNINGS}",
        "10m",
        "warning",
        "The Controller is rate-limiting calls persistently",
        "At least 10 per-User or per-address rate-limit warnings in 15 minutes, for 10 "
        "minutes. A single quota rejection is expected and does not alert.",
        "controller",
    ),
    Rule(
        "NestMigrationNotCompleted",
        'max(nest_migration_status{status!="completed"}) == 1 and on() '
        f"(time() - max(nest_migration_deploy_timestamp_seconds) > {MIGRATION_GRACE_SECONDS})",
        "5m",
        "critical",
        "The deployment's migration is not known to have completed",
        "15 minutes after the Controller container was created, the migration is failed, "
        "stuck, still running, absent, stale or unknown. Run nest-deploy-doctor.py "
        "migrate-status; do not remove containers or volumes to clear this.",
        "migration-status",
    ),
    Rule(
        "NestMonitorCollectorFailing",
        f"time() - max by (collector) "
        f'(nest_monitor_last_success_timestamp_seconds{{collector=~"{LOG_COLLECTORS}"}}) '
        f"> {COLLECTOR_STALE_SECONDS}",
        "5m",
        "warning",
        "A Nest monitoring collector has not succeeded for 15 minutes",
        "Its signals are stale: the container it reads is missing or not running, or "
        "Docker cannot be queried. See the collector's journal.",
        "collectors",
    ),
    Rule(
        "NestMonitorCollectorMissing",
        f'(count(nest_monitor_last_run_timestamp_seconds{{collector=~"{ALL_COLLECTORS}"}}) < 4) '
        f'or absent(nest_monitor_last_run_timestamp_seconds{{collector=~"{ALL_COLLECTORS}"}})',
        "30m",
        "warning",
        "Nest monitoring series are missing",
        "At least one collector's text file is not being scraped: the timer never ran, "
        "the textfile directory is not mounted, or the scrape job is down.",
        "collectors",
    ),
)


def _labels(rule: Rule) -> dict[str, str]:
    return {"service": "nest", "severity": rule.severity}


def _annotations(rule: Rule) -> dict[str, str]:
    return {
        "summary": rule.summary,
        "description": rule.description,
        "runbook_url": f"{RUNBOOK}#{rule.anchor}",
    }


def prometheus() -> str:
    # JSON strings are valid YAML double-quoted scalars.
    q = json.dumps
    lines = [
        "# Generated by deploy/monitoring/nest_alerts.py; edit that file, not this one.",
        "groups:",
        f"  - name: {GROUP}",
        f"    interval: {INTERVAL}",
        "    rules:",
    ]
    for rule in RULES:
        lines += [
            f"      - alert: {rule.name}",
            f"        expr: {q(rule.expr)}",
            f"        for: {rule.duration}",
            "        labels:",
            *(f"          {k}: {q(v)}" for k, v in _labels(rule).items()),
            "        annotations:",
            *(f"          {k}: {q(v)}" for k, v in _annotations(rule).items()),
        ]
    return "\n".join(lines) + "\n"


_UID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def grafana(datasource_uid: str, contact_point: str, paused: bool = False) -> dict:
    if not _UID.match(datasource_uid):
        raise ValueError("the data source UID has 1-40 letters, digits, '-' or '_'")
    if not contact_point.strip() or any(ord(c) < 32 or c in "${}" for c in contact_point):
        raise ValueError("name an existing contact point (no control characters, $ or braces)")
    rules = []
    for rule in RULES:
        rules.append(
            {
                "uid": rule.uid,
                "title": rule.name,
                "condition": "C",
                "data": [
                    {
                        "refId": "A",
                        "relativeTimeRange": {"from": 900, "to": 0},
                        "datasourceUid": datasource_uid,
                        "model": {
                            "refId": "A",
                            "expr": f"count({rule.expr}) or vector(0)",
                            "instant": True,
                            "range": False,
                            "intervalMs": 60000,
                            "maxDataPoints": 43200,
                        },
                    },
                    {
                        "refId": "C",
                        "relativeTimeRange": {"from": 0, "to": 0},
                        "datasourceUid": "__expr__",
                        "model": {
                            "refId": "C",
                            "type": "threshold",
                            "expression": "A",
                            "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
                        },
                    },
                ],
                # The query always answers (`or vector(0)`): no data means Prometheus
                # or the data source itself is failing, which must not look healthy.
                "noDataState": "Alerting",
                "execErrState": "Error",
                "for": rule.duration,
                "labels": _labels(rule),
                "annotations": _annotations(rule),
                "isPaused": paused,
                "notification_settings": {"receiver": contact_point},
            }
        )
    return {
        "apiVersion": 1,
        "groups": [
            {"orgId": 1, "name": GROUP, "folder": FOLDER, "interval": INTERVAL, "rules": rules}
        ],
    }


def main(argv: list[str] | None = None) -> int:
    top = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("prometheus")
    render = commands.add_parser("grafana")
    render.add_argument("--datasource-uid", required=True)
    render.add_argument("--contact-point", required=True)
    render.add_argument(
        "--paused", action="store_true", help="provision the rules paused (a first rollout)"
    )
    args = top.parse_args(argv)
    if args.command == "prometheus":
        sys.stdout.write(prometheus())
        return 0
    try:
        sys.stdout.write(
            json.dumps(grafana(args.datasource_uid, args.contact_point, args.paused), indent=2)
        )
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
