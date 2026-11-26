"""
The Quickstart must stay runnable and agree with the SDK.

`docs/source/quickstart.md` and `README.md` show the same four steps (install, log
in, create `main.py`, run). These tests run that `main.py` through a real local
Controller and check its documented output, check that the documented connection
settings are accepted by the SDK's own loader and send the hosted session, and
check that the docs navigation lists every page exactly once.
"""

import os
import re
import subprocess
import sys
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
        self.assertNotIn("NEST_PROJECT_ID", happy_path)
        self.assertIn("@nest.remote(cpu=1)", quickstart_main())
        self.assertNotIn("gpu", quickstart_main())

    def test_install_starts_a_fresh_uv_project(self):
        install = blocks(section(QUICKSTART.read_text(), "## 1. Install"), "bash")[0]
        self.assertEqual(
            install.split("\n")[:3],
            [
                "uv init --python 3.14 hello-nest",
                "cd hello-nest",
                'uv add "nest @ git+https://github.com/cerulean-works/nest@main"',
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
