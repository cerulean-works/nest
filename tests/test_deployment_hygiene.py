"""Repository checks for deployment credentials and SSH host-key verification.

These read tracked files with `git ls-files`, so they skip outside a Git
checkout (for example inside the Controller image, where only `tests/` is mounted).
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

# Content that is a credential wherever it appears.
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "Tailscale key": re.compile(r"\btskey-[a-z]+-[A-Za-z0-9]{6,}"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
}

# Settings that disable or weaken OpenSSH host-key verification, in config
# files ("Key value"), -o options ("Key=value"), and YAML/env assignments.
WEAK_HOST_KEY_PATTERNS = (
    re.compile(
        r"StrictHostKeyChecking[\"']?\s*[=:\s]\s*[\"']?(?:no|off|false|accept-new|ask)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:Global|User)KnownHostsFile[\"']?\s*[=:\s]\s*[\"']?/dev/null", re.IGNORECASE),
    re.compile(r"\bssh-keyscan\b[^\n]*>>?\s*\S*known_hosts"),
)

# Deployment credentials belong outside Git (see .gitignore).
CREDENTIAL_PATHS = re.compile(
    r"(?:^|/)(?:\.env(?:\..+)?|id_(?:rsa|ecdsa|ed25519|dsa)(?:_sk)?|.*\.(?:pem|key|p12|pfx))$"
    r"|^(?:ssh|secrets|files|tailscale-state)/"
)
CREDENTIAL_PATH_EXCEPTIONS = {".env.example"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [name for name in result.stdout.split("\0") if name]


def is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path], cwd=ROOT, check=False
    )
    return result.returncode == 0


def git_checkout() -> bool:
    if not shutil.which("git"):
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == ROOT


@unittest.skipUnless(git_checkout(), "requires a Git checkout of the repository")
class DeploymentHygieneTests(unittest.TestCase):
    def text_files(self):
        for name in tracked_files():
            path = ROOT / name
            if name == SELF or not path.is_file():
                continue
            try:
                yield name, path.read_text()
            except UnicodeDecodeError:
                continue

    def test_no_credentials_are_tracked(self):
        findings = [
            f"{name}:{text.count(chr(10), 0, match.start()) + 1}: {label}"
            for name, text in self.text_files()
            for label, pattern in SECRET_PATTERNS.items()
            for match in pattern.finditer(text)
        ]
        self.assertEqual(findings, [], "Credentials must not be committed")

    def test_no_credential_files_are_tracked(self):
        findings = [
            name
            for name in tracked_files()
            if CREDENTIAL_PATHS.search(name) and name not in CREDENTIAL_PATH_EXCEPTIONS
        ]
        self.assertEqual(findings, [], "Deployment credential files must not be committed")

    def test_host_key_verification_is_never_disabled(self):
        findings = [
            f"{name}:{text.count(chr(10), 0, match.start()) + 1}: {match.group(0)}"
            for name, text in self.text_files()
            for pattern in WEAK_HOST_KEY_PATTERNS
            for match in pattern.finditer(text)
        ]
        self.assertEqual(findings, [], "SSH host-key verification must stay enabled")

    def test_tracked_ssh_configs_require_verified_host_keys(self):
        configs = [
            name
            for name in tracked_files()
            if re.search(r"(?:^|/)ssh_config[^/]*$|(?:^|/)\.ssh/config$", name)
        ]
        self.assertIn("docker/ssh_config.tailscale.example", configs)
        for name in configs:
            with self.subTest(config=name):
                text = (ROOT / name).read_text()
                self.assertRegex(text, r"(?m)^\s*StrictHostKeyChecking\s+yes\s*$")
                self.assertRegex(text, r"(?m)^\s*BatchMode\s+yes\s*$")

    def test_deployment_credential_locations_are_ignored(self):
        for path in (
            ".env",
            ".env.production",
            "ssh/id_ed25519",
            "ssh/known_hosts",
            "secrets/tailscale-authkey",
            "files/nest-ssh/id_ed25519",
            "tailscale-state/tailscaled.state",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_ignored(path), f"{path} must be ignored by Git")
        self.assertFalse(is_ignored(".env.example"))

    def test_docker_build_context_excludes_credentials(self):
        rules = [
            line.strip()
            for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(rules[0], "**", "The build context must start from deny-all")
        included = [rule[1:] for rule in rules if rule.startswith("!")]
        for rule in included:
            with self.subTest(rule=rule):
                self.assertFalse(
                    re.match(r"(?:\*\*/)?(?:\.env|ssh|secrets|files|tailscale-state)\b", rule),
                    f"{rule} would add deployment credentials to the image",
                )


class HostKeyPatternTests(unittest.TestCase):
    """The scanners must catch the settings they are meant to forbid."""

    def test_weak_host_key_settings_are_detected(self):
        for sample in (
            "    StrictHostKeyChecking no",
            "ssh -o StrictHostKeyChecking=no host",
            "ssh -o 'StrictHostKeyChecking=accept-new' host",
            "StrictHostKeyChecking: off",
            "    UserKnownHostsFile /dev/null",
            "-o GlobalKnownHostsFile=/dev/null",
            "ssh-keyscan login >> ~/.ssh/known_hosts",
        ):
            with self.subTest(sample=sample):
                self.assertTrue(any(p.search(sample) for p in WEAK_HOST_KEY_PATTERNS))

    def test_verified_host_key_settings_are_allowed(self):
        for sample in (
            "    StrictHostKeyChecking yes",
            "ssh -o StrictHostKeyChecking=yes host",
            "    UserKnownHostsFile ~/.ssh/known_hosts",
            "never use `accept-new` or `/dev/null` to fix errors",
        ):
            with self.subTest(sample=sample):
                self.assertFalse(any(p.search(sample) for p in WEAK_HOST_KEY_PATTERNS))

    def test_credentials_are_detected(self):
        samples = {
            "private key": "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
            "Tailscale key": "TS_AUTHKEY=tskey-auth-" + "k" * 12,
        }
        for label, sample in samples.items():
            with self.subTest(label=label):
                self.assertTrue(SECRET_PATTERNS[label].search(sample))


if __name__ == "__main__":
    unittest.main()
