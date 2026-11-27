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
RELEASING = DOCS / "releasing.md"
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


FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)


def blocks(text: str, language: str) -> list[str]:
    """Every fenced code block of `language` in `text`."""
    return [body for lang, body in FENCE.findall(text) if lang == language]


def markdown_sections(text: str) -> list[str]:
    """`text` split at Markdown headings, ignoring `#` lines inside code fences."""
    parts, current, fenced = [], [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and re.match(r"#{1,6} ", line):
            parts.append("".join(current))
            current = []
        current.append(line)
    parts.append("".join(current))
    return parts


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
        text = INSTALLATION.read_text()
        install = section(text, "## Install with uv")
        tag = f"v{package_version()}"
        self.assertIn(f'uv add "nest @ git+https://github.com/cerulean-works/nest@{tag}"', install)
        self.assertIn(f'rev = "{tag}"', install)
        self.assertIn(f'version = "{package_version()}"', install)
        # The condition comes before the command, so nobody runs it without reading it.
        self.assertLess(install.index(RELEASES_PAGE), install.index(f"nest@{tag}"))
        self.assertIn("planned", install)
        pip = section(text, "### Install with pip")
        self.assertLess(
            pip.index(RELEASES_PAGE),
            pip.index(f'pip install "git+https://github.com/cerulean-works/nest.git@{tag}"'),
        )

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

    def test_every_tag_install_names_the_published_condition_first(self):
        # Within the section that offers the tag, the condition (the Releases page, or
        # the Install section that names it) comes before the first `@<tag>`.
        tag = f"@v{package_version()}"
        conditions = (RELEASES_PAGE, "installation#releases-and-main")
        for page in user_facing_pages():
            text = page.read_text()
            for part in markdown_sections(text):
                if tag not in part:
                    continue
                with self.subTest(page=page.name, section=part.split("\n", 1)[0]):
                    first = part.index(tag)
                    self.assertTrue(
                        any(c in part[:first] for c in conditions),
                        f"{page.name}: {tag} is offered before saying how to check it exists",
                    )

    def assert_release_first_then_labeled_main(self, install: str):
        # The immutable tagged docs must give readers the released SDK (#118: a later
        # `main` can be incompatible with the Release deployment). `@main` is only a
        # separately labeled development fallback, after the release command.
        release = f'uv add "nest @ git+https://github.com/cerulean-works/nest@v{package_version()}"'
        main = 'uv add "nest @ git+https://github.com/cerulean-works/nest@main"'
        self.assertIn(release, blocks(install, "bash")[0])
        self.assertLess(install.index(release), install.index(main))
        fallback = install[install.index(release) + len(release) : install.index(main)]
        self.assertIn("Main deployment", fallback)
        self.assertRegex(fallback, r"(?i)not listed yet|not published")
        self.assertIn("incompatible", install[install.index(main) :] + fallback)

    def test_the_quickstart_installs_the_release_first(self):
        self.assert_release_first_then_labeled_main(
            section(QUICKSTART.read_text(), "## 1. Install")
        )

    def test_the_readme_installs_the_release_first(self):
        readme = section(README.read_text(), "## Quickstart")
        install = readme[readme.index("**1. Install**") : readme.index("**2. Log in**")]
        self.assert_release_first_then_labeled_main(install)

    def test_the_install_page_offers_main_only_as_the_development_build(self):
        text = INSTALLATION.read_text()
        main = 'uv add "nest @ git+https://github.com/cerulean-works/nest@main"'
        self.assertNotIn(main, section(text, "## Install with uv").split("### ", 1)[0])
        releases = section(text, "### Releases and `@main`")
        self.assertIn(main, releases)
        self.assertIn("Development build", releases[: releases.index(main)])
        self.assertIn("Main deployment", releases)


@requires_docs
class ReleaseGateTests(unittest.TestCase):
    """The maintainer's a5 decision (#116, #96): the tag waits for these gates."""

    GATES = (
        "https://nest.cerulean.works",
        "grpc.nest.cerulean.works:443",
        "no `NEST_*` settings and no Tailnet",
        "#96",
        "H09",
        "staging or canary",
        "#118",
        "main.nest.cerulean.works",
    )

    def notes(self) -> str:
        return (ROOT / "docs" / "release-notes" / f"v{package_version()}.md").read_text()

    def test_draft_notes_are_blocked_on_every_gate(self):
        notes = self.notes()
        self.assertIn("## Release status: BLOCKED", notes)
        gates = section(notes, "## Release status: BLOCKED")
        for gate in self.GATES:
            with self.subTest(gate=gate):
                self.assertIn(gate, gates)

    def test_draft_notes_document_the_legacy_origin(self):
        legacy = section(self.notes(), "## Legacy sign-in origin")
        self.assertIn("auth.nest.cerulean.works", legacy)
        self.assertIn("nest login", legacy)
        self.assertIn("invitation", legacy)

    def test_draft_notes_do_not_exclude_h09_from_the_release(self):
        notes = self.notes()
        self.assertNotIn("not part of this release", notes)
        self.assertNotIn("open the public gateway as part of this release", notes)

    def test_the_checklist_gates_the_tag(self):
        checklist = section(RELEASING.read_text(), "## Release-prep checklist")
        gates = checklist[
            checklist.index("**Release-specific gates.**") : checklist.index("**The tag.**")
        ]
        for gate in self.GATES:
            with self.subTest(gate=gate):
                self.assertIn(gate, gates)


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
