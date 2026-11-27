"""
The Quickstart must stay runnable and agree with the SDK.

`docs/source/quickstart.md` and `README.md` show the same four steps (install, log
in, create `main.py`, run). These tests run that `main.py` through a real local
Controller and check its documented output, check that the documented connection
settings are accepted by the SDK's own loader and send the hosted session, and
check that the docs navigation lists every page exactly once.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

from test_controller import controller_at_free_port

from nest.config import HOSTED_AUTH_URL, load_client_config

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "source"
QUICKSTART = DOCS / "quickstart.md"
README = ROOT / "README.md"
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

# The Controller image smoke test mounts only `tests/`; the Tests workflow, which has
# the checkout, runs these.
requires_docs = unittest.skipUnless(DOCS.is_dir(), "requires the repository's docs/source")


def section(text: str, heading: str) -> str:
    """The text after `heading` up to the next heading of the same level."""
    start = text.index(heading) + len(heading)
    level = heading.split(" ", 1)[0]
    following = re.search(rf"^{re.escape(level)} ", text[start:], re.MULTILINE)
    return text[start : start + following.start()] if following else text[start:]


def blocks(text: str, language: str) -> list[str]:
    """Every fenced code block of `language` in `text`."""
    return [body for lang, body in FENCE.findall(text) if lang == language]


def quickstart_main() -> str:
    return blocks(section(QUICKSTART.read_text(), "## 3. Create main.py"), "python")[0]


def exports(script: str) -> dict[str, str]:
    """The `export NAME="value"` lines of a shell block, without comments."""
    found = {}
    for line in script.splitlines():
        match = re.match(r'export (\w+)="?([^"\s]*)"?', line.strip())
        if match:
            found[match.group(1)] = match.group(2)
    return found


@requires_docs
class QuickstartTests(unittest.TestCase):
    def test_steps_are_in_order(self):
        headings = re.findall(r"^## (.+)$", QUICKSTART.read_text(), re.MULTILINE)
        steps = [h for h in headings if re.match(r"\d\. ", h)]
        self.assertEqual(steps, ["1. Install", "2. Log in", "3. Create main.py", "4. Run"])
        self.assertLess(headings.index("Connection settings"), headings.index("1. Install"))

    def test_readme_shows_the_same_main_py(self):
        readme = blocks(section(README.read_text(), "## Quickstart"), "python")
        self.assertEqual(readme, [quickstart_main()])

    def test_happy_path_needs_no_project_id_and_no_gpu(self):
        text = QUICKSTART.read_text()
        happy_path = text[text.index("## Connection settings") : text.index("## 4. Run")]
        # Clearing a leftover override is fine; setting one is not needed.
        self.assertNotRegex(happy_path, r"(export |\b)NEST_PROJECT_ID=")
        self.assertIn("@nest.remote(cpu=1)", quickstart_main())
        self.assertNotIn("gpu", quickstart_main())

    def test_install_starts_a_fresh_uv_project(self):
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        install = blocks(section(QUICKSTART.read_text(), "## 1. Install"), "bash")[0]
        self.assertEqual(
            install.split("\n")[:3],
            [
                "uv init --python 3.14 hello-nest",
                "cd hello-nest",
                f'uv add "nest @ git+https://github.com/cerulean-works/nest@v{version}"',
            ],
        )

    def test_main_py_prints_42_twice_through_a_controller(self):
        with controller_at_free_port() as (_, _stub, address):
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("NEST_") or key == "NEST_LOG_LEVEL"
            }
            env["NEST_CONTROLLER_ADDRESS"] = address
            result = subprocess.run(
                [sys.executable, "-c", quickstart_main()],
                check=False,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = blocks(section(QUICKSTART.read_text(), "## 4. Run"), "text")[0]
        self.assertEqual(result.stdout, expected)
        self.assertEqual(expected, "42\n42\n")

    def test_private_connection_settings_carry_the_hosted_session(self):
        settings = exports(
            blocks(section(QUICKSTART.read_text(), "## Connection settings"), "bash")[0]
        )
        self.assertEqual(
            set(settings), {"NEST_AUTH_URL", "NEST_CONTROLLER_ADDRESS", "NEST_CONTROLLER_TLS"}
        )
        config = load_client_config(settings)
        self.assertEqual(config.address, settings["NEST_CONTROLLER_ADDRESS"])
        self.assertFalse(config.tls)
        self.assertEqual(config.auth_url, HOSTED_AUTH_URL)
        self.assertIsNone(config.project_id)

    def test_readme_connection_settings_match_the_quickstart(self):
        readme = exports("\n".join(blocks(section(README.read_text(), "## Quickstart"), "bash")))
        quickstart = exports(
            blocks(section(QUICKSTART.read_text(), "## Connection settings"), "bash")[0]
        )
        self.assertEqual(readme, quickstart)

    def test_the_checked_in_example_uses_the_same_resources(self):
        example = (ROOT / "examples" / "hosted_remote.py").read_text()
        self.assertIn("@nest.remote(cpu=1)\ndef compute(x):", example)
        self.assertIn("return x * 2", example)


def r5_environment(ca_file: str) -> dict[str, str]:
    """The settings the rollout runbook's staging run R5 leaves in a terminal."""
    runbook = (DOCS / "hosted-rollout.md").read_text()
    block = blocks(section(runbook, "### R5. Signed in, hosted SDK, one execution"), "bash")[0]
    settings = {
        name: value.replace("<domain>", "example.org") for name, value in exports(block).items()
    }
    settings["NEST_CONTROLLER_TLS_CA_FILE"] = ca_file
    return settings


def config_after(script: str, inherited: dict[str, str]) -> subprocess.CompletedProcess:
    """
    Runs the setting lines of a documented shell block in a terminal that already
    has `inherited`, then loads the SDK's client settings as `.remote()` would.
    Network and `uv run` lines are left out.
    """
    lines = [
        line
        for line in script.replace("\\\n", " ").splitlines()
        if line.strip().startswith(("export ", "unset "))
    ]
    probe = (
        "import json; from nest.config import load_client_config as load; c = load(); "
        "print(json.dumps({'address': c.address, 'tls': c.tls, 'auth_url': c.auth_url, "
        "'project_id': c.project_id, 'profile': c.profile}))"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("NEST_")}
    env.update(inherited)
    return subprocess.run(
        ["bash", "-c", "\n".join([*lines, f'exec "{sys.executable}" -c "{probe}"'])],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@requires_docs
class ConnectionSwitchTests(unittest.TestCase):
    """
    Each documented connection block must work in a terminal that a previous
    connection (the staging run R5, the hosted profile, an old Project override)
    left settings in, without weakening TLS or the session binding.
    """

    def setUp(self):
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
            handle.write("-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n")
        self.addCleanup(os.unlink, handle.name)
        self.leftovers = {
            **r5_environment(handle.name),
            "NEST_PROJECT_ID": "00000000-0000-4000-8000-000000000001",
        }

    def load(self, heading: str, inherited: dict[str, str]) -> dict:
        script = "\n".join(blocks(section(QUICKSTART.read_text(), heading), "bash"))
        result = config_after(script, inherited)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_r5_settings_are_what_the_runbook_says(self):
        result = config_after("", self.leftovers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["address"], "grpc.staging.example.org:443")

    def test_the_private_path_without_cleanup_is_refused(self):
        quickstart = blocks(section(QUICKSTART.read_text(), "## Connection settings"), "bash")[0]
        exports_only = "\n".join(l for l in quickstart.splitlines() if l.startswith("export "))
        result = config_after(exports_only, self.leftovers)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NEST_CONTROLLER_TLS_CA_FILE", result.stderr)

    def test_private_path_after_r5(self):
        config = self.load("## Connection settings", self.leftovers)
        self.assertEqual(config["address"], "nest-controller.tail8e5ba1.ts.net:50051")
        self.assertFalse(config["tls"])
        self.assertEqual(config["auth_url"], HOSTED_AUTH_URL)
        self.assertIsNone(config["project_id"])

    def test_private_path_after_the_hosted_profile(self):
        config = self.load("## Connection settings", {"NEST_PROFILE": "hosted"})
        self.assertEqual(config["profile"], "custom")
        self.assertEqual(config["address"], "nest-controller.tail8e5ba1.ts.net:50051")
        self.assertEqual(config["auth_url"], HOSTED_AUTH_URL)

    def test_self_hosted_after_r5(self):
        config = self.load("## Use a self-hosted Nest", self.leftovers)
        self.assertEqual(config["address"], "controller.example.com:50051")
        self.assertTrue(config["tls"])
        self.assertEqual(config["auth_url"], "https://nest.example.com")
        self.assertIsNone(config["project_id"])

    def test_local_flow_after_r5(self):
        config = self.load(
            "## Local development flow (no session)", {**self.leftovers, "NEST_PROFILE": "hosted"}
        )
        self.assertEqual(config["address"], "127.0.0.1:50051")
        self.assertIsNone(config["auth_url"])
        self.assertIsNone(config["project_id"])

    def test_local_controller_page_after_r5(self):
        page = (DOCS / "local-controller.md").read_text()
        script = "\n".join(b for b in blocks(page, "bash") if "unset NEST_" in b)
        result = config_after(script, {**self.leftovers, "NEST_PROFILE": "hosted"})
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config["address"], "127.0.0.1:50051")
        self.assertFalse(config["tls"])
        self.assertIsNone(config["auth_url"])

    def test_every_documented_cleanup_works_after_r5(self):
        """Any doc block that clears settings must leave a terminal the SDK accepts."""
        pages = [README, *sorted(DOCS.rglob("*.md"))]
        found = 0
        for page in pages:
            for script in blocks(page.read_text(), "bash"):
                if not re.search(r"^unset NEST_", script, re.MULTILINE):
                    continue
                found += 1
                with self.subTest(page=page.name, block=script.splitlines()[0]):
                    result = config_after(script, {**self.leftovers, "NEST_PROFILE": "hosted"})
                    self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(found, 5)

    def test_example_hosted_block_after_the_private_path(self):
        example = (ROOT / "examples" / "hosted_remote.py").read_text()
        private, hosted = re.findall(
            r"^Private path.*?\n\n(.*?)\n\n|^Hosted profile.*?\n\n(.*?)\n\n",
            example,
            re.MULTILINE | re.DOTALL,
        )
        script = textwrap.dedent(private[0]) + "\n" + textwrap.dedent(hosted[1])
        result = config_after(script, self.leftovers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["profile"], "hosted")

    def test_readme_private_path_after_r5(self):
        script = "\n".join(blocks(section(README.read_text(), "## Quickstart"), "bash"))
        result = config_after(script, self.leftovers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["auth_url"], HOSTED_AUTH_URL)


@requires_docs
class NavigationTests(unittest.TestCase):
    def test_every_page_is_listed_exactly_once(self):
        listed = re.findall(
            r"^\s*- local: (\S+)$", (DOCS / "_toctree.yml").read_text(), re.MULTILINE
        )
        pages = sorted(str(p.relative_to(DOCS).with_suffix("")) for p in DOCS.rglob("*.md"))
        self.assertEqual(sorted(listed), pages)
        self.assertEqual(len(listed), len(set(listed)))

    def test_user_pages_come_before_operator_pages(self):
        toctree = (DOCS / "_toctree.yml").read_text()
        guide = toctree[: toctree.index("reference:")]
        for page in ("quickstart", "get-access", "project-selection", "console"):
            self.assertLess(guide.index(f"local: {page}\n"), guide.index("Operator guide"))
        for page in ("dokploy", "project-onboarding", "hosted-rollout"):
            self.assertGreater(guide.index(f"local: {page}\n"), guide.index("Operator guide"))


if __name__ == "__main__":
    unittest.main()
