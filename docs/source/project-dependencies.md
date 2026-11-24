# Use project dependencies

Nest supports project-specific Python environments managed by uv.

Given a project containing:

```text
my-project/
├── pyproject.toml
├── uv.lock
└── main.py
```

Use the `project` parameter:

```python
import nest


@nest.remote(gpu=1, project=".")
def compute():
    """Formats a number with the project's humanize dependency."""
    import humanize

    return humanize.intcomma(1234567)


print(compute.remote())
```

Nest transfers the project's `pyproject.toml` and `uv.lock` to the Controller.

The HPC backend prepares an isolated Python environment and caches it for reuse.

**Note:** Automatic project source-code synchronization is not yet supported. Project-local modules and data files are not automatically transferred.

## Prepare the dependency files

Add dependencies and update the lockfile before making the call. For the example
above, run this in `my-project/`:

```bash
uv add humanize
```

Run your script from that directory when using `project="."`, or pass an explicit
project path. Nest resolves the path when the function is decorated and reads
`pyproject.toml` and `uv.lock` when the remote call is made. Both files must exist
and be nonempty.

Import optional dependencies inside the remote function, as in the example.
The user environment is created from the lockfile without installing the project
itself or its development dependencies. The Slurm backend caches environments
using the dependency files and the container runtime identity.

Dependencies pinned to a public Git repository, such as
`uv add "six @ git+https://github.com/benjaminp/six@<commit>"`, are installed
from the lockfile like PyPI packages, provided the host's runtime image includes
Git and compute nodes can reach the repository. Private Git repositories are not
supported yet: builds use no host or operator credentials. See
[Build and roll out the HPC runtime image](runtime-image#git-dependencies-supported-scope).

Environment preparation applies to Slurm execution. A local Controller executes
in its own Python environment and does not install the supplied project
dependencies.

Environment creation and queueing count toward the client's RPC deadline. See
[Environment variables and defaults](configuration) for the current limits.
