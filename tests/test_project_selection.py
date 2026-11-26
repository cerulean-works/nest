"""E07: authorized Project selection when NEST_PROJECT_ID is unset.

The first part runs against a recording Controller and needs no database: the
order of precedence (explicit, saved, only eligible, prompt, clear failure), how
saved choices are scoped by account, sign-in origin and Controller, the
preferences file, a Controller from before Project selection, and `nest projects`.

The second part serves the real `ExecutionService` with session authentication and
registry admission against PostgreSQL and the fake login node: which Projects are
eligible (role, Provider approval, Platform grant, membership), and that the
Controller authorizes every execution again, so a revoked Project is refused and
nothing is submitted even when the SDK had chosen it.
"""

import io
import json
import os
import stat
import types
import uuid
from concurrent import futures
from contextlib import redirect_stderr, redirect_stdout
from typing import cast
from unittest import mock

import grpc
from hosted_fixture import HostedCase, RecordingController, serve, session_for
from test_registry_admission import OPS, _Base, browser

from nest import cli as nest_cli
from nest import project_selection as selection
from nest.authz import AllowedProjects
from nest.client import execute_remote
from nest.config import load_client_config
from nest.controller import ExecutionService
from nest.db import PlatformStore
from nest.errors import ProjectSelectionError, RemoteAccessDeniedError
from nest.login import NestSession
from nest.project_selection import (
    PreferenceKey,
    PreferenceStore,
    list_eligible,
    preference_key,
    select_project,
)
from nest.proto import execution_pb2, execution_pb2_grpc

AUTH_URL = "https://nest.example.com"
RESOURCES = types.SimpleNamespace(gpu=0, cpu=0)


# A lambda is pickled by value: the worker on the (fake) cluster cannot import tests.
answer_42 = lambda: 42


class _Selection(HostedCase):
    def setUp(self):
        super().setUp()
        self.alpha, self.beta = str(uuid.uuid4()), str(uuid.uuid4())
        self.session = session_for(AUTH_URL)
        self.store.save(self.session)

    def controller(self, *projects, **options):
        controller = RecordingController(projects=projects, **options)
        port = serve(self, controller)
        self.env = {"NEST_CONTROLLER_ADDRESS": f"127.0.0.1:{port}", "NEST_AUTH_URL": AUTH_URL}
        return controller

    def config(self, **extra):
        return load_client_config({**os.environ, **self.env, **extra})

    def select(self, *, interactive=False, answers=(), user=None, **extra):
        config = self.config(**extra)
        replies = iter(answers)
        out = io.StringIO()
        with grpc.insecure_channel(config.address) as channel:
            choice = select_project(
                config,
                channel,
                self.session.access_token,
                user or self.session.user_id,
                interactive=interactive,
                out=out,
                read=lambda _prompt: next(replies),
            )
        self.out = out.getvalue()
        return choice


class PrecedenceTests(_Selection):
    def test_explicit_project_wins_without_asking_the_controller(self):
        controller = self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        choice = self.select(NEST_PROJECT_ID=self.beta.upper())
        assert choice is not None
        self.assertEqual((choice.project_id, choice.source), (self.beta, "NEST_PROJECT_ID"))
        self.assertEqual(controller.calls, [])

    def test_only_eligible_project_is_used_and_not_saved(self):
        self.controller((self.alpha, "alpha"))
        choice = self.select()
        assert choice is not None
        self.assertEqual((choice.project_id, choice.source), (self.alpha, "only eligible"))
        self.assertFalse(os.path.exists(self.preferences))

    def test_none_eligible_fails_clearly(self):
        self.controller()
        with self.assertRaises(ProjectSelectionError) as caught:
            self.select()
        self.assertEqual(caught.exception.reason, "none_eligible")
        self.assertIn("invite you", str(caught.exception))

    def test_several_without_a_terminal_fail_and_list_them(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta\x1b[31m"))
        with self.assertRaises(ProjectSelectionError) as caught:
            self.select(interactive=False)
        error = caught.exception
        self.assertEqual(error.reason, "ambiguous")
        self.assertEqual(set(error.project_ids), {self.alpha, self.beta})
        self.assertIn("NEST_PROJECT_ID", str(error))
        self.assertIn(self.beta, str(error))
        self.assertNotIn("\x1b", str(error))  # names cannot inject terminal codes

    def test_prompt_saves_the_choice_privately_and_it_is_reused(self):
        controller = self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        choice = self.select(interactive=True, answers=["0", "x", "2"])
        assert choice is not None
        self.assertEqual((choice.project_id, choice.source), (self.beta, "chosen"))
        self.assertIn("Enter a number from 1 to 2", self.out)
        self.assertEqual(stat.S_IMODE(os.stat(self.preferences).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.preferences)).st_mode), 0o700)
        with open(self.preferences) as handle:
            saved = json.load(handle)
        self.assertNotIn(self.session.access_token, json.dumps(saved))
        self.assertNotIn(self.session.refresh_token, json.dumps(saved))
        # Reused without asking; the Controller is still asked whether it is eligible.
        again = self.select(interactive=True, answers=[])
        assert again is not None
        self.assertEqual((again.project_id, again.source), (self.beta, "saved"))
        self.assertEqual(controller.methods(), ["ListEligibleProjects"] * 2)

    def test_project_id_can_be_typed_at_the_prompt(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        choice = self.select(interactive=True, answers=[self.alpha.upper()[:8], self.alpha])
        assert choice is not None
        self.assertEqual(choice.project_id, self.alpha)

    def test_saved_project_no_longer_eligible_is_forgotten(self):
        self.controller((self.alpha, "alpha"))
        key = preference_key(self.config(), self.session.user_id)
        assert key is not None
        PreferenceStore(self.preferences).set(key, self.beta)
        choice = self.select()
        assert choice is not None
        self.assertEqual((choice.project_id, choice.source), (self.alpha, "only eligible"))
        self.assertIn("no longer available", self.out)
        self.assertIsNone(PreferenceStore(self.preferences).get(key))

    def test_choices_are_scoped_by_account_origin_and_controller(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        config = self.config()
        store = PreferenceStore(self.preferences)
        mine = preference_key(config, self.session.user_id)
        assert mine is not None
        store.set(mine, self.beta)
        other_user = PreferenceKey(mine.auth_url, mine.controller, str(uuid.uuid4()))
        other_origin = PreferenceKey("https://other.example", mine.controller, mine.user_id)
        other_controller = PreferenceKey(mine.auth_url, "127.0.0.1:1", mine.user_id)
        for key in (other_user, other_origin, other_controller):
            self.assertIsNone(store.get(key))
        store.set(other_user, self.alpha)
        self.assertEqual(store.get(mine), self.beta)
        # Another account on the same Nest is asked again.
        with self.assertRaises(ProjectSelectionError):
            self.select(user=uuid.uuid4())
        self.assertTrue(store.remove(mine))
        self.assertFalse(store.remove(mine))
        self.assertEqual(store.get(other_user), self.alpha)

    def test_unreadable_preferences_are_ignored(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        os.makedirs(os.path.dirname(self.preferences), mode=0o700, exist_ok=True)
        with open(self.preferences, "w") as handle:
            handle.write("{not json")
        choice = self.select(interactive=True, answers=["1"])
        assert choice is not None
        self.assertEqual(choice.project_id, self.alpha)
        with open(self.preferences) as handle:
            self.assertEqual(json.load(handle)["kind"], "nest-preferences")

    def test_no_session_or_no_origin_means_no_listing(self):
        controller = self.controller((self.alpha, "alpha"))
        config = self.config()
        with grpc.insecure_channel(config.address) as channel:
            self.assertIsNone(select_project(config, channel, None, None, interactive=False))
        no_origin = load_client_config(
            {"NEST_CONTROLLER_ADDRESS": self.env["NEST_CONTROLLER_ADDRESS"]}
        )
        with grpc.insecure_channel(config.address) as channel:
            self.assertIsNone(select_project(no_origin, channel, "t" * 20, uuid.uuid4()))
        self.assertEqual(controller.calls, [])

    def test_controller_without_authentication_needs_no_project(self):
        self.controller((self.alpha, "alpha"), require_session=False)
        self.assertIsNone(self.select())


class _OldController(execution_pb2_grpc.ExecutionServiceServicer):
    """A Controller from before Project selection."""

    def __init__(self):
        self.metadata: list[dict] = []

    def Execute(self, request, context):
        self.metadata.append(dict(context.invocation_metadata()))
        context.abort(
            grpc.StatusCode.INVALID_ARGUMENT, "Name the Project to run in with NEST_PROJECT_ID"
        )


class CompatibilityTests(_Selection):
    def test_old_controller_answers_as_before(self):
        old = _OldController()
        port = serve(self, old)
        self.env = {"NEST_CONTROLLER_ADDRESS": f"127.0.0.1:{port}", "NEST_AUTH_URL": AUTH_URL}
        self.assertIsNone(self.select())
        with (
            mock.patch.dict(os.environ, self.env),
            self.assertRaisesRegex(grpc.RpcError, "NEST_PROJECT_ID"),
        ):
            execute_remote(answer_42, RESOURCES, None)
        self.assertNotIn("x-nest-project-id", old.metadata[0])
        with (
            grpc.insecure_channel(f"127.0.0.1:{port}") as channel,
            self.assertRaises(ProjectSelectionError) as caught,
        ):
            list_eligible(channel, "t" * 20)
        self.assertEqual(caught.exception.reason, "unsupported")


class ProjectsCommandTests(_Selection):
    def nest(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), redirect_stdout(out), redirect_stderr(err):
            code = nest_cli.main(["projects", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_use_current_forget(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        code, out, _ = self.nest("list", "--json")
        self.assertEqual(code, 0)
        listed = json.loads(out)
        self.assertEqual([p["id"] for p in listed["projects"]], [self.alpha, self.beta])
        self.assertIsNone(listed["saved_project_id"])
        code, _, err = self.nest("current")
        self.assertEqual(code, 1)
        self.assertIn("several Nest Projects", err)
        code, _, err = self.nest("use", str(uuid.uuid4()))
        self.assertEqual(code, 1)
        self.assertIn("not one you may run in", err)
        self.assertEqual(self.nest("use", self.beta)[0], 0)
        code, out, _ = self.nest("current", "--json")
        self.assertEqual(json.loads(out), {"project_id": self.beta, "source": "saved"})
        code, out, _ = self.nest("list")
        self.assertIn(f"* {self.beta}", out)
        self.assertEqual(self.nest("forget")[1].strip(), "Forgot the saved Project.")
        self.assertEqual(self.nest("forget")[1].strip(), "No Project was saved here.")
        # NEST_PROJECT_ID overrides any saved choice.
        self.env["NEST_PROJECT_ID"] = self.alpha
        code, out, _ = self.nest("current")
        self.assertEqual(out.strip(), f"{self.alpha} (NEST_PROJECT_ID)")

    def pick(self, *answers, interactive=True, env=None):
        """`nest projects select` with `answers` typed at the prompt (EOF after them)."""
        replies = iter(answers)

        def read(_prompt=""):
            try:
                return next(replies)
            except StopIteration:
                raise EOFError from None

        with (
            mock.patch.object(selection, "is_interactive", return_value=interactive),
            mock.patch("builtins.input", read),
            mock.patch.dict(os.environ, env or {}),
        ):
            return self.nest("select")

    def saved(self):
        key = preference_key(self.config(), self.session.user_id)
        assert key is not None
        return PreferenceStore(self.preferences).get(key)

    def test_select_by_number_or_unique_name_and_save(self):
        self.controller((self.alpha, "Protein folding"), (self.beta, "Course: HPC 101", "admin"))
        code, out, err = self.pick("9", "course: hpc 101")
        self.assertEqual(code, 0, err)
        self.assertIn("1. Protein folding (member)", err)
        self.assertIn("2. Course: HPC 101 (admin)", err)
        self.assertIn("Enter a number from 1 to 2", err)
        self.assertIn(f"The SDK now runs in Course: HPC 101 (admin) [{self.beta}]", out)
        self.assertEqual(self.saved(), self.beta)
        # The saved one is marked next time, and a number selects too.
        code, _, err = self.pick("1")
        self.assertIn("2. Course: HPC 101 (admin)  (saved)", err)
        self.assertEqual((code, self.saved()), (0, self.alpha))
        # `list`, `current` and `use` keep their output for scripts.
        code, out, _ = self.nest("current", "--json")
        self.assertEqual(json.loads(out), {"project_id": self.alpha, "source": "saved"})

    def test_duplicate_names_are_told_apart_and_never_guessed(self):
        twin = self.alpha[:8] + str(uuid.uuid4())[8:]  # same 8-character prefix
        self.controller((self.alpha, "lab"), (twin, "Lab"), (self.beta, "other"))
        code, _, err = self.pick("lab", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("Several Projects are named 'lab'; enter the number", err)
        prefixes = [line.split("id ")[1] for line in err.splitlines() if "  id " in line]
        self.assertEqual(len(prefixes), 2)
        self.assertNotEqual(prefixes[0], prefixes[1])
        self.assertTrue(self.alpha.startswith(prefixes[0]) and twin.startswith(prefixes[1]))
        self.assertEqual(self.saved(), twin)

    def test_cancel_changes_nothing(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        self.assertEqual(self.nest("use", self.alpha)[0], 0)
        for answers in ([""], ["q"], []):  # Enter, q, end of input
            code, _, err = self.pick(*answers)
            self.assertEqual(code, 1)
            self.assertIn("cancelled; nothing was saved", err)
            self.assertEqual(self.saved(), self.alpha)
        with (
            mock.patch("builtins.input", side_effect=KeyboardInterrupt),
            mock.patch.object(selection, "is_interactive", return_value=True),
        ):
            code, _, _ = self.nest("select")
        self.assertEqual((code, self.saved()), (130, self.alpha))

    def test_without_a_terminal_or_eligible_project_nothing_is_saved(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        code, _, err = self.pick("1", interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal", err)
        self.assertIn("nest projects use <ID>", err)
        self.assertIn(self.beta, err)
        self.assertIsNone(self.saved())
        self.controller()
        code, _, err = self.pick("1")
        self.assertEqual(code, 1)
        self.assertIn("no Project that may run", err)
        self.assertIsNone(self.saved())

    def test_override_is_explained_and_kept(self):
        self.controller((self.alpha, "alpha"), (self.beta, "beta"))
        self.env["NEST_PROJECT_ID"] = self.alpha
        code, out, err = self.pick("2")
        self.assertEqual(code, 0, err)
        self.assertIn(f"NEST_PROJECT_ID is set to {self.alpha}", err)
        self.assertIn("still overrides it", out)
        self.assertEqual(self.saved(), self.beta)
        code, out, _ = self.nest("current")
        self.assertEqual(out.strip(), f"{self.alpha} (NEST_PROJECT_ID)")

    def test_choice_revoked_while_asking_is_not_saved(self):
        controller = self.controller((self.alpha, "alpha"), (self.beta, "beta"))

        def read(_prompt=""):
            controller.projects = [p for p in controller.projects if p[0] != self.beta]
            return "2"

        with (
            mock.patch.object(selection, "is_interactive", return_value=True),
            mock.patch("builtins.input", read),
        ):
            code, _, err = self.nest("select")
        self.assertEqual(code, 1)
        self.assertIn("no longer one you may run in", err)
        self.assertIsNone(self.saved())

    def test_controller_without_a_session_origin(self):
        port = serve(self, RecordingController())
        self.env = {"NEST_CONTROLLER_ADDRESS": f"127.0.0.1:{port}"}
        code, _, err = self.nest("list")
        self.assertEqual(code, 2)
        self.assertIn("NEST_AUTH_URL", err)


class EligibilityTests(_Base):
    """Eligibility and per-execution revalidation against the real Controller."""

    auth_url = "http://127.0.0.1:9"

    def setUp(self):
        super().setUp()
        self.platform_store = PlatformStore(self.db)
        self.service = ExecutionService(
            self.backend,
            executor="slurm",
            jobs=self.jobs,
            access=self.access,
            admission=self.admission,
            local_execution=False,
        )
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(self.service, server)
        self.port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        self.list_stub = execution_pb2_grpc.ExecutionServiceStub(
            grpc.insecure_channel(f"127.0.0.1:{self.port}")
        )
        home = self.enterContext(__import__("tempfile").TemporaryDirectory())
        os.chmod(home, 0o700)
        self.env = {
            "XDG_CONFIG_HOME": home,
            "NEST_CONTROLLER_ADDRESS": f"127.0.0.1:{self.port}",
            "NEST_AUTH_URL": self.auth_url,
            "NEST_PROJECT_ID": "",
        }
        self.preferences = os.path.join(home, "nest", "preferences.json")
        self.sign_in(self.member, self.token_member)

    def sign_in(self, user_id, token):
        from datetime import UTC, datetime, timedelta

        from nest.login import CredentialStore

        now = datetime.now(UTC)
        session = NestSession(
            auth_url=self.auth_url,
            user_id=user_id,
            session_id=uuid.uuid4(),
            scope="jobs:run",
            access_token=token,
            refresh_token="refresh-" + "r" * 30,
            access_expires_at=now + timedelta(minutes=9),
            refresh_expires_at=now + timedelta(hours=1),
            session_expires_at=now + timedelta(hours=1),
        )
        CredentialStore(os.path.join(self.env["XDG_CONFIG_HOME"], "nest", "credentials.json")).save(
            session
        )

    def eligible(self, token=None):
        response = self.list_stub.ListEligibleProjects(
            execution_pb2.ListEligibleProjectsRequest(),
            timeout=30,
            metadata=[("authorization", f"Bearer {token or self.token_member}")],
        )
        self.assertTrue(response.projects_required)
        return [(uuid.UUID(p.id), p.role) for p in response.projects]

    def run_remote(self, **env):
        with mock.patch.dict(os.environ, {**self.env, **env}):
            return execute_remote(answer_42, RESOURCES, None)

    def test_eligibility_follows_role_approval_grant_and_membership(self):
        self.assertEqual(self.eligible(), [(self.project, "member")])
        self.assertEqual(self.eligible(self.token_outsider), [(self.other, "owner")])
        # A viewer may not submit, so the Project is not eligible.
        self.projects.add_member(browser(self.outsider), self.other, self.member, "viewer")
        self.assertEqual(self.eligible(), [(self.project, "member")])
        self.projects.change_role(browser(self.outsider), self.other, self.member, "member")
        self.assertEqual(
            sorted(self.eligible()), sorted([(self.project, "member"), (self.other, "member")])
        )
        # Platform grant revoked: not eligible from the next call on.
        self.platform_store.revoke_project(self.platform_id, self.other, **OPS)
        self.assertEqual(self.eligible(), [(self.project, "member")])
        # Provider no longer approves the Project.
        self.access.authorizer.providers = AllowedProjects([str(self.other)])
        self.assertEqual(self.eligible(), [])
        # Listing records no refusal per Project.
        self.assertFalse(
            [e for e in self.audit.events(event="run.submit") if e.outcome == "denied"]
        )

    def test_a_project_without_a_default_account_is_not_eligible(self):
        # Granted and approved, but no Slurm account a default submission could
        # charge: admission would refuse it no_account, so it is not offered.
        self.projects.add_member(browser(self.outsider), self.other, self.member, "member")
        self.accounts.unlink_project(self.other, self.platform_id, "proj", **OPS)
        self.assertEqual(self.eligible(), [(self.project, "member")])

        # So the one usable Project is chosen without asking, not reported ambiguous.
        self.assertEqual(self.run_remote(), 42)
        # A saved choice of the accountless Project is forgotten and the usable one runs.
        PreferenceStore(self.preferences).set(self.key(), str(self.other))
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_remote(), 42)
        self.assertIn("no longer available", err.getvalue())
        self.assertIsNone(PreferenceStore(self.preferences).get(self.key()))
        self.assertEqual(
            [r["project_id"] for r in self.sql("SELECT project_id FROM executions")],
            [self.project, self.project],
        )

        # NEST_PROJECT_ID still wins, and admission still refuses it authoritatively.
        sent = len(self.sbatch_calls())
        with self.assertRaises(Exception) as caught:
            self.run_remote(NEST_PROJECT_ID=str(self.other))
        self.assertIn("account", str(caught.exception).lower())
        self.assertEqual(len(self.sbatch_calls()), sent)

        # Listing an accountless Project records nothing (the refused run above did).
        resolutions = len(self.audit.events(event="slurm_account.resolve"))
        self.assertEqual(self.eligible(), [(self.project, "member")])
        self.assertEqual(len(self.audit.events(event="slurm_account.resolve")), resolutions)

        # Once a default account is linked, it is eligible again.
        self.accounts.link_project(
            self.other, self.platform_id, "proj", modes=["A"], default=True, **OPS
        )
        self.assertEqual(
            sorted(self.eligible()), sorted([(self.project, "member"), (self.other, "member")])
        )

    def test_listing_needs_an_accepted_session_and_a_project_scope(self):
        with self.assertRaises(grpc.RpcError) as caught:
            self.list_stub.ListEligibleProjects(
                execution_pb2.ListEligibleProjectsRequest(), timeout=30
            )
        self.assertEqual(cast(grpc.Call, caught.exception).code(), grpc.StatusCode.UNAUTHENTICATED)
        scopeless = self.token(self.member, scope="profile:read")
        with self.assertRaises(grpc.RpcError) as caught:
            self.eligible(scopeless)
        self.assertEqual(
            cast(grpc.Call, caught.exception).code(), grpc.StatusCode.PERMISSION_DENIED
        )
        open_service = ExecutionService(self.backend, executor="slurm")
        context = mock.Mock()
        context.invocation_metadata.return_value = ()
        response = open_service.ListEligibleProjects(
            execution_pb2.ListEligibleProjectsRequest(), context
        )
        self.assertFalse(response.projects_required)

    def test_sdk_runs_in_the_only_project_then_asks_then_revalidates(self):
        self.assertEqual(self.run_remote(), 42)
        [attempt] = self.attempts()
        executions = self.sql("SELECT project_id FROM executions")
        self.assertEqual(executions, [{"project_id": self.project}])
        self.assertTrue(attempt["slurm_job_id"])

        # A second eligible Project: without a terminal, nothing is submitted.
        self.projects.add_member(browser(self.outsider), self.other, self.member, "member")
        with self.assertRaises(ProjectSelectionError):
            self.run_remote()
        self.assertEqual(len(self.sbatch_calls()), 1)

        # On a terminal the choice is asked once and saved.
        with (
            mock.patch("nest.project_selection.is_interactive", return_value=True),
            mock.patch("builtins.input", return_value="2"),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(self.run_remote(), 42)
        self.assertEqual(self.run_remote(), 42)
        projects = [
            r["project_id"]
            for r in self.sql("SELECT project_id FROM executions ORDER BY created_at")
        ]
        self.assertEqual(projects, [self.project, self.other, self.other])

        # Removed from the saved Project: forgotten, and the only one left is used.
        self.projects.remove_member(browser(self.outsider), self.other, self.member)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_remote(), 42)
        self.assertIn("no longer available", err.getvalue())
        self.assertFalse(PreferenceStore(self.preferences).get(self.key()))

    def key(self):
        config = load_client_config(self.env)
        key = preference_key(config, self.member)
        assert key is not None
        return key

    def test_revoked_between_listing_and_execution_is_refused_and_forgotten(self):
        self.projects.add_member(browser(self.outsider), self.other, self.member, "member")
        PreferenceStore(self.preferences).set(self.key(), str(self.other))
        sent = len(self.sbatch_calls())
        real = selection.list_eligible

        def listed_then_revoked(*args, **kwargs):
            listed = real(*args, **kwargs)
            # The membership ends after the Controller said the Project is eligible.
            self.projects.remove_member(browser(self.outsider), self.other, self.member)
            return listed

        with (
            mock.patch.object(selection, "list_eligible", listed_then_revoked),
            self.assertRaises(RemoteAccessDeniedError) as caught,
        ):
            self.run_remote()
        self.assertIn("forgotten", str(caught.exception))
        self.assertEqual(len(self.sbatch_calls()), sent)  # nothing was submitted
        self.assertIsNone(PreferenceStore(self.preferences).get(self.key()))

    def test_explicit_project_is_authorized_every_time(self):
        with self.assertRaises(RemoteAccessDeniedError):
            self.run_remote(NEST_PROJECT_ID=str(self.other))
        self.assertEqual(self.sbatch_calls(), [])
        self.assertEqual(self.run_remote(NEST_PROJECT_ID=str(self.project)), 42)
        self.projects.remove_member(browser(self.owner), self.project, self.member)
        with self.assertRaises(RemoteAccessDeniedError):
            self.run_remote(NEST_PROJECT_ID=str(self.project))
        self.assertEqual(len(self.sbatch_calls()), 1)
        # A revoked session is refused before any Project is listed or run.
        self.sessions_revoked()
        with self.assertRaises(Exception) as caught:
            self.run_remote()
        self.assertIn("Nest session", str(caught.exception))
        self.assertEqual(len(self.sbatch_calls()), 1)

    def sessions_revoked(self):
        self.sql(
            "UPDATE user_sessions SET revoked_at = now(), revoked_reason = 'logout' "
            "WHERE user_id = %s",
            self.member,
        )
