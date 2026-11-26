"""
The API reference pages must introspect cleanly at runtime.

The Docs build (doc-builder) imports every `[[autodoc]]` object in
`docs/source` and calls `inspect.signature` on it and on each public method it
documents. On Python 3.14, annotations are evaluated lazily (PEP 649), so a
name imported only under `TYPE_CHECKING` raises `NameError` there, even though
the module imports, the type checker passes and the tests that call the code
pass. These tests take that same path for every documented object, so such an
annotation fails here instead of on the main Docs build.
"""

import importlib
import inspect
import re
import unittest
from pathlib import Path

import nest

DOCS = Path(__file__).resolve().parent.parent / "docs" / "source"
AUTODOC = re.compile(r"^\[\[autodoc\]\]\s+(\S+)", re.MULTILINE)


def autodoc_entries() -> list[tuple[str, str]]:
    """Every `[[autodoc]]` target in the docs, with the page that names it."""
    entries = []
    for page in sorted(DOCS.rglob("*.md")):
        for name in AUTODOC.findall(page.read_text()):
            entries.append((str(page.relative_to(DOCS)), name))
    return entries


def resolve(name: str) -> object:
    """Find `name` inside the `nest` package, as doc-builder does."""
    parts = name.split(".")
    if parts[0] == nest.__name__:
        parts = parts[1:]
    obj: object = nest
    for part in parts:
        child = getattr(obj, part, None)
        if child is None and inspect.ismodule(obj):
            child = importlib.import_module(f"{obj.__name__}.{part}")
        if child is None:
            raise AttributeError(f"{name}: no attribute {part!r}")
        obj = child
    return obj


def documented_methods(cls: type) -> dict[str, object]:
    """The public methods doc-builder documents for `cls` by default."""
    methods = {}
    for attr in dir(cls):
        if attr.startswith("_"):
            continue
        member = getattr(cls, attr)
        if callable(member) and not isinstance(member, type) and getattr(member, "__doc__", None):
            methods[attr] = member
    return methods


def signature(obj: object) -> inspect.Signature | None:
    """`inspect.signature` with doc-builder's tolerance: only `ValueError` is skipped."""
    try:
        return inspect.signature(obj)  # ty: ignore[invalid-argument-type]
    except ValueError:
        return None


class AutodocIntrospectionTests(unittest.TestCase):
    def test_the_docs_name_autodoc_objects(self):
        names = {name for _, name in autodoc_entries()}
        # The objects whose annotations broke the main Docs build (#107).
        self.assertIn("backends.transport.ControllerSSHTransport", names)
        self.assertIn("backends.slurm.SlurmBackend", names)

    def test_every_documented_signature_evaluates(self):
        for page, name in autodoc_entries():
            with self.subTest(page=page, object=name):
                obj = resolve(name)
                signature(obj)
                if isinstance(obj, type):
                    for attr, method in documented_methods(obj).items():
                        with self.subTest(page=page, object=f"{name}.{attr}"):
                            signature(method)

    def test_controller_transport_signature_names_slurm_config(self):
        from nest.backends.transport import ControllerSSHTransport

        parameter = inspect.signature(ControllerSSHTransport).parameters["config"]
        self.assertIn("SlurmConfig", str(parameter.annotation))


if __name__ == "__main__":
    unittest.main()
