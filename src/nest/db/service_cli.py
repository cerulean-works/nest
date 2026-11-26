"""
`nest-db service-principal`: operator management of service principals (B09).

    nest-db service-principal list
    nest-db service-principal show   PRINCIPAL
    nest-db service-principal create NAME [--description TEXT]
                              [--max-active-sessions N] [--max-exchanges-per-hour N]
    nest-db service-principal trust  PRINCIPAL --audience AUD --repository OWNER/NAME
                              --repository-id N --repository-owner-id N
                              --ref REF [--ref REF ...] --workflow PATH [--workflow PATH ...]
                              [--environment NAME ...]
    nest-db service-principal grant  PRINCIPAL --project-id UUID --capabilities CAP,CAP
                              [--platform PLATFORM]
    nest-db service-principal revoke PRINCIPAL
    nest-db service-principal revoke-policy  POLICY_ID
    nest-db service-principal revoke-grant   GRANT_ID
    nest-db service-principal revoke-session SESSION_ID
    nest-db service-principal prune  # forget expired workload token uses
    (every change also takes --operator NAME --reason TEXT)

PRINCIPAL is a principal's name or ID. Every change is audited with the operator
name, the reason and the PostgreSQL role.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .connection import Database
from .platforms import PlatformStore
from .service_principals import (
    SERVICE_CAPABILITIES,
    ServiceGrant,
    ServicePrincipal,
    ServicePrincipalError,
    ServicePrincipalStore,
    TrustPolicy,
)


def add_parser(commands, change: argparse.ArgumentParser) -> None:
    """Adds `service-principal` to `nest-db`'s subcommands."""
    parser = commands.add_parser(
        "service-principal",
        help="manage CI service principals, their trust policies and grants",
        description=(
            "Service principals authenticate CI workloads, such as GitHub Actions jobs, "
            "without a browser. They are not Users and get no Controller HPC authority. "
            "Authenticated by the database credentials in NEST_DATABASE_*; every change is "
            "audited with the operator name, the reason and the PostgreSQL role."
        ),
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list every service principal")
    show = actions.add_parser("show", help="show a principal, its policies, grants, sessions")
    show.add_argument("principal", metavar="PRINCIPAL")
    create = actions.add_parser("create", parents=[change], help="create a service principal")
    create.add_argument("name", metavar="NAME")
    create.add_argument("--description")
    create.add_argument("--max-active-sessions", type=int, default=4)
    create.add_argument("--max-exchanges-per-hour", type=int, default=60)
    trust = actions.add_parser(
        "trust", parents=[change], help="trust a GitHub Actions workflow, by exact values"
    )
    trust.add_argument("principal", metavar="PRINCIPAL")
    trust.add_argument("--audience", required=True, help="NEST_WORKLOAD_OIDC_AUDIENCE")
    trust.add_argument("--repository", required=True, metavar="OWNER/NAME")
    trust.add_argument("--repository-id", required=True, metavar="N")
    trust.add_argument("--repository-owner-id", required=True, metavar="N")
    trust.add_argument(
        "--ref", action="append", required=True, help="refs/heads/... or refs/tags/..."
    )
    trust.add_argument(
        "--workflow", action="append", required=True, help=".github/workflows/<file>.yml"
    )
    trust.add_argument(
        "--environment",
        action="append",
        default=[],
        help="a deployment environment; without one, only jobs without an environment match",
    )
    grant = actions.add_parser("grant", parents=[change], help="grant capabilities on a Project")
    grant.add_argument("principal", metavar="PRINCIPAL")
    grant.add_argument("--project-id", required=True, metavar="UUID")
    grant.add_argument("--platform", metavar="PLATFORM", help="a Platform slug or ID")
    grant.add_argument(
        "--capabilities",
        required=True,
        metavar="CAP,CAP",
        help=f"from {','.join(SERVICE_CAPABILITIES)}; run.* have no effect yet",
    )
    revoke = actions.add_parser("revoke", parents=[change], help="revoke a principal for good")
    revoke.add_argument("principal", metavar="PRINCIPAL")
    for name, what in (
        ("revoke-policy", "a trust policy and its sessions"),
        ("revoke-grant", "a grant"),
        ("revoke-session", "one service session"),
    ):
        item = actions.add_parser(name, parents=[change], help=f"revoke {what}")
        item.add_argument("id", metavar="ID")
    actions.add_parser("prune", help="forget workload token uses past their replay window")


def _print_principal(principal: ServicePrincipal) -> None:
    print(
        f"{principal.name} id={principal.id} status={principal.status} "
        f"max_active_sessions={principal.max_active_sessions} "
        f"max_exchanges_per_hour={principal.max_exchanges_per_hour}"
        + (f" description={json.dumps(principal.description)}" if principal.description else "")
    )


def _print_policy(policy: TrustPolicy) -> None:
    print(
        f"  policy {policy.id} status={policy.status} repository={policy.repository} "
        f"repository_id={policy.repository_id} owner_id={policy.repository_owner_id} "
        f"audience={json.dumps(policy.audience)} refs={','.join(policy.refs)} "
        f"workflows={','.join(policy.workflows)} "
        f"environments={','.join(policy.environments) or '(none)'}"
    )


def _print_grant(grant: ServiceGrant) -> None:
    print(
        f"  grant {grant.id} status={grant.status} project={grant.project_id} "
        f"platform={grant.platform_id or '-'} capabilities={','.join(grant.capabilities)}"
    )


def run(database: Database, args: argparse.Namespace) -> int:
    """Runs one `nest-db service-principal` action on an open, verified database."""
    store = ServicePrincipalStore(database)
    audit: dict[str, Any] = {
        "operator": getattr(args, "operator", None),
        "reason": getattr(args, "reason", None),
    }
    try:
        if args.action == "list":
            for principal in store.list_principals():
                _print_principal(principal)
        elif args.action == "show":
            principal = store.get_principal(args.principal)
            if principal is None:
                print("No such service principal", file=sys.stderr)
                return 1
            _print_principal(principal)
            for policy in store.policies(principal.id):
                _print_policy(policy)
            for grant in store.grants(principal.id):
                _print_grant(grant)
            for session in store.active_sessions(principal.id):
                print(
                    f"  session {session.id} expires_at={session.expires_at.isoformat()} "
                    f"run_id={session.provenance.get('run_id', '-')} "
                    f"ref={session.provenance.get('ref', '-')}"
                )
        elif args.action == "create":
            _print_principal(
                store.create_principal(
                    args.name,
                    description=args.description,
                    max_active_sessions=args.max_active_sessions,
                    max_exchanges_per_hour=args.max_exchanges_per_hour,
                    **audit,
                )
            )
        elif args.action == "trust":
            _print_policy(
                store.add_trust_policy(
                    args.principal,
                    audience=args.audience,
                    repository=args.repository,
                    repository_id=args.repository_id,
                    repository_owner_id=args.repository_owner_id,
                    refs=args.ref,
                    workflows=args.workflow,
                    environments=args.environment,
                    **audit,
                )
            )
        elif args.action == "grant":
            platform_id = None
            if args.platform:
                platform = PlatformStore(database).get_platform(args.platform)
                if platform is None:
                    print("No such Platform", file=sys.stderr)
                    return 1
                platform_id = platform.id
            _print_grant(
                store.grant(
                    args.principal,
                    args.project_id,
                    [c.strip() for c in args.capabilities.split(",") if c.strip()],
                    platform_id=platform_id,
                    **audit,
                )
            )
        elif args.action == "revoke":
            _print_principal(store.revoke_principal(args.principal, **audit))
        elif args.action == "revoke-policy":
            _print_policy(store.revoke_policy(args.id, **audit))
        elif args.action == "revoke-grant":
            if not args.id.isdecimal():
                raise ValueError("A grant ID is a number")
            _print_grant(store.revoke_grant(int(args.id), **audit))
        elif args.action == "revoke-session":
            if not store.revoke_session(args.id, operator=args.operator, note=args.reason):
                print("No change: the session is not active", file=sys.stderr)
                return 1
            print(f"Service session {args.id} revoked")
        else:
            print(f"pruned {store.prune_token_uses()} workload token uses")
        return 0
    except ServicePrincipalError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
