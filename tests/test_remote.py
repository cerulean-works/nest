"""Unit tests for the ``remote`` decorator.

These tests exercise the three dispatch paths of a decorated function:

* calling the decorated object directly -> runs the wrapped function locally
* calling ``.local(...)``                -> runs the wrapped function locally
* calling ``.remote(...)``               -> delegates to ``execute_remote``

The remote path's ``execute_remote`` collaborator is stubbed so no gRPC call
is made; only the decorator's own behavior is under test.
"""

import sys
from pathlib import Path

import pytest

import nest
from nest.remote import RemoteFunction, Resources, remote

# ``nest.remote`` the attribute is the re-exported decorator function (see
# ``nest/__init__.py``), so fetch the actual submodule to patch its globals.
remote_module = sys.modules["nest.remote"]


class TestDecoratorConstruction:
    """How the decorator wraps a function and records its configuration."""

    def test_returns_remote_function(self):
        """The decorator wraps the function in a ``RemoteFunction``."""

        @remote()
        def fn(x):
            return x

        assert isinstance(fn, RemoteFunction)

    def test_preserves_metadata(self):
        """``update_wrapper`` copies the wrapped function's identity."""

        @remote()
        def compute(x):
            """Docstring of compute."""
            return x

        assert compute.__name__ == "compute"
        assert compute.__doc__ == "Docstring of compute."

    def test_stores_resources(self):
        """``gpu``/``cpu`` kwargs are recorded as ``Resources``."""

        @remote(gpu=2, cpu=4)
        def fn():
            return None

        assert fn.resources == Resources(gpu=2, cpu=4)

    def test_resources_default(self):
        """Default resources are 0 GPU and 1 CPU."""

        @remote()
        def fn():
            return None

        assert fn.resources == Resources(gpu=0, cpu=1)

    def test_project_none_by_default(self):
        """Without ``project``, the resolved project is ``None``."""

        @remote()
        def fn():
            return None

        assert fn.project is None

    def test_project_is_resolved(self):
        """A ``project`` argument is expanded and resolved to an absolute path."""

        @remote(project="~/some/project")
        def fn():
            return None

        expected = Path("~/some/project").expanduser().resolve()
        assert fn.project == expected
        assert fn.project.is_absolute()

    def test_public_api_exposes_remote(self):
        """``nest.remote`` is the decorator exported from the package."""

        assert nest.remote is remote


class TestDirectCall:
    """Calling the decorated object directly runs the function locally."""

    def test_executes_locally(self):
        """Calling the decorated object runs the wrapped function in-process."""

        @remote()
        def double(x):
            return x * 2

        assert double(21) == 42

    def test_does_not_hit_remote(self, monkeypatch):
        """A direct call must not reach ``execute_remote``."""

        sentinel = object()

        def boom(*args, **kwargs):
            raise AssertionError("execute_remote should not be called")

        monkeypatch.setattr(remote_module, "execute_remote", boom)

        @remote()
        def identity(x):
            return x

        assert identity(sentinel) is sentinel

    def test_forwards_args_and_kwargs(self):
        """Positional and keyword arguments reach the wrapped function."""

        @remote()
        def fn(a, b, c=0):
            return (a, b, c)

        assert fn(1, 2, c=3) == (1, 2, 3)


class TestLocalCall:
    """``.local(...)`` runs the function locally."""

    def test_executes_locally(self):
        """``.local(...)`` runs the wrapped function in-process."""

        @remote()
        def double(x):
            return x * 2

        assert double.local(21) == 42

    def test_does_not_hit_remote(self, monkeypatch):
        """``.local`` must not reach ``execute_remote``."""

        sentinel = object()

        def boom(*args, **kwargs):
            raise AssertionError("execute_remote should not be called")

        monkeypatch.setattr(remote_module, "execute_remote", boom)

        @remote()
        def identity(x):
            return x

        assert identity.local(sentinel) is sentinel

    def test_forwards_args_and_kwargs(self):
        """``.local`` forwards positional and keyword arguments unchanged."""

        @remote()
        def fn(a, b, c=0):
            return (a, b, c)

        assert fn.local(1, 2, c=3) == (1, 2, 3)


class TestLocalExecutionPropagatesExceptions:
    """Both local dispatch paths re-raise the wrapped function's exceptions."""

    @pytest.mark.parametrize(
        "call",
        [lambda f: f(), lambda f: f.local()],
        ids=["direct", "local"],
    )
    @pytest.mark.parametrize(
        "exc_type",
        [ValueError, RuntimeError, KeyError, TypeError],
    )
    def test_propagates_unchanged(self, call, exc_type):
        """Whatever the wrapped function raises propagates to the caller.

        Covered for both local dispatch paths (``__call__`` and ``.local``)
        and a range of exception types, since propagation is type-agnostic.
        """

        @remote()
        def fn():
            raise exc_type("boom")

        with pytest.raises(exc_type, match="boom"):
            call(fn)


class TestRemoteCall:
    """``.remote(...)`` delegates to ``execute_remote`` (stubbed)."""

    def test_delegates_to_execute_remote(self, monkeypatch):
        """``.remote(...)`` calls ``execute_remote`` and returns its result."""

        calls = []

        def fake_execute_remote(func, resources, project, *args, **kwargs):
            calls.append((func, resources, project, args, kwargs))
            return "remote-result"

        monkeypatch.setattr(remote_module, "execute_remote", fake_execute_remote)

        @remote(gpu=1, cpu=2, project="~/proj")
        def add(a, b):
            return a + b

        result = add.remote(3, 4, flag=True)

        assert result == "remote-result"
        assert len(calls) == 1

        func, resources, project, args, kwargs = calls[0]
        assert func is add.func
        assert resources == Resources(gpu=1, cpu=2)
        assert project == Path("~/proj").expanduser().resolve()
        assert args == (3, 4)
        assert kwargs == {"flag": True}

    def test_does_not_execute_function_locally(self, monkeypatch):
        """``.remote`` hands off serialization; it does not run the function."""

        ran = []

        @remote()
        def fn():
            ran.append(True)
            return "local"

        monkeypatch.setattr(
            remote_module, "execute_remote", lambda *a, **k: "remote"
        )

        assert fn.remote() == "remote"
        assert ran == []  # wrapped function was never invoked locally

    def test_passes_none_project_when_unset(self, monkeypatch):
        """With no ``project``, ``execute_remote`` receives ``None``."""

        captured = {}

        def fake_execute_remote(func, resources, project, *args, **kwargs):
            captured["project"] = project
            return None

        monkeypatch.setattr(remote_module, "execute_remote", fake_execute_remote)

        @remote()
        def fn():
            return None

        fn.remote()

        assert captured["project"] is None

    def test_propagates_execute_remote_errors(self, monkeypatch):
        """Errors from ``execute_remote`` propagate to the caller."""

        def boom(*args, **kwargs):
            raise ConnectionError("controller unavailable")

        monkeypatch.setattr(remote_module, "execute_remote", boom)

        @remote()
        def fn():
            return None

        with pytest.raises(ConnectionError, match="controller unavailable"):
            fn.remote()
