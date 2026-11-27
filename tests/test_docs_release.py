"""
Release-prep docs must agree with the package and never send users to a missing tag.

A `v*` tag publishes the tagged commit's own docs, and doc-builder renders `main` and
a tag from the same text (see `docs/source/releasing.md`, *How `main` and tagged docs
differ*). These checks keep the package version, `uv.lock`, the pinned install
commands and the draft release notes consistent, and require every release-pinned
command to carry the "published on the Releases page" condition, so the same text is
true before and after the tag.
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "source"
INSTALLATION = DOCS / "installation.md"
QUICKSTART = DOCS / "quickstart.md"
README = ROOT / "README.md"
WORKER_SCRIPT = ROOT / "scripts" / "worker.sbatch"
RELEASES_PAGE = "https://github.com/cerulean-works/nest/releases"
TAG_INSTALL = re.compile(r"cerulean-works/nest(?:\.git)?@(v\d[\w.]*)")

requires_docs = unittest.skipUnless(DOCS.is_dir(), "requires the repository's docs/source")


def package_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    level = heading.split(" ", 1)[0]
    following = re.search(rf"^{re.escape(level)} ", text[start:], re.MULTILINE)
    return text[start : start + following.start()] if following else text[start:]


def user_facing_pages() -> list[Path]:
    return [README, *sorted(DOCS.rglob("*.md")), *sorted((ROOT / "examples").glob("*.py"))]


@requires_docs
class ReleaseVersionTests(unittest.TestCase):
    def test_uv_lock_matches_the_package_version(self):
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        (entry,) = [p for p in lock["package"] if p["name"] == "nest"]
        self.assertEqual(entry["version"], package_version())
        self.assertEqual(entry["source"], {"editable": "."})

    def test_the_version_is_a_pep440_pre_release_matching_a_v_tag(self):
        self.assertRegex(package_version(), r"^\d+\.\d+\.\d+a\d+$")

    def test_installation_pins_the_tag_of_this_version(self):
        releases = section(INSTALLATION.read_text(), "### Releases and `@main`")
        tag = f"v{package_version()}"
        self.assertIn(f'uv add "nest @ git+https://github.com/cerulean-works/nest@{tag}"', releases)
        self.assertIn(
            f'pip install "git+https://github.com/cerulean-works/nest.git@{tag}"', releases
        )
        self.assertIn(f'rev = "{tag}"', releases)
        self.assertIn(f'version = "{package_version()}"', releases)
        # The condition comes before the command, so nobody runs it without reading it.
        self.assertLess(releases.index(RELEASES_PAGE), releases.index(f"nest@{tag}"))
        self.assertIn("planned", releases)

    def test_example_lock_entries_show_the_package_version(self):
        text = INSTALLATION.read_text()
        self.assertEqual(
            set(re.findall(r'^version = "([^"]+)"$', text, re.MULTILINE)), {package_version()}
        )
        self.assertIn(f"Updated nest v{package_version()} (", text)

    def test_readme_names_the_version_and_the_condition(self):
        early_access = section(README.read_text(), "## Early Access")
        self.assertIn(f"`{package_version()}`", early_access)
        self.assertIn(f"`v{package_version()}`", early_access)
        self.assertIn(RELEASES_PAGE, early_access)

    def test_draft_release_notes_exist_for_this_version(self):
        notes = ROOT / "docs" / "release-notes" / f"v{package_version()}.md"
        self.assertTrue(notes.is_file(), notes)
        self.assertIn(f"v{package_version()}", notes.read_text())


@requires_docs
class TagInstallTests(unittest.TestCase):
    def test_only_the_current_release_is_offered_as_a_tag_install(self):
        tag = f"v{package_version()}"
        for page in user_facing_pages():
            for found in TAG_INSTALL.findall(page.read_text()):
                with self.subTest(page=page.name):
                    self.assertEqual(found, tag)

    def test_every_tag_install_names_the_published_condition(self):
        # A page that offers the tag must also say how to tell whether it exists.
        for page in user_facing_pages():
            text = page.read_text()
            if TAG_INSTALL.search(text) or f"@v{package_version()}" in text:
                with self.subTest(page=page.name):
                    self.assertIn(RELEASES_PAGE, text)

    def test_the_quickstart_still_installs_main(self):
        install = section(QUICKSTART.read_text(), "## 1. Install")
        self.assertIn('uv add "nest @ git+https://github.com/cerulean-works/nest@main"', install)
        self.assertNotIn(f'nest@v{package_version()}"', install)


@requires_docs
class QuickstartGpuWordingTests(unittest.TestCase):
    def test_a_cpu_only_call_is_not_described_as_allocating_no_gpu(self):
        run = section(QUICKSTART.read_text(), "## 4. Run")
        self.assertNotRegex(run, r"\(`cpu=1`\) and no GPU")
        self.assertIn("holds", run)
        self.assertIn("admission-limits#what-is-reserved", run)

    def test_the_quoted_worker_default_matches_the_script(self):
        directive = re.search(
            r"^#SBATCH (--gpus-per-node=\d+)$", WORKER_SCRIPT.read_text(), re.MULTILINE
        )
        assert directive is not None, "scripts/worker.sbatch has no --gpus-per-node default"
        self.assertIn(f"`#SBATCH {directive.group(1)}`", QUICKSTART.read_text())


if __name__ == "__main__":
    unittest.main()
