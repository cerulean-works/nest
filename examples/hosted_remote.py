# /// script
# requires-python = ">=3.14"
# dependencies = ["nest @ git+https://github.com/cerulean-works/nest@main"]
# ///
"""
The smallest program that runs a function on Cerulean-hosted Nest, with uv.

Private path (over the Controller's Tailnet; available today, see
docs/source/quickstart.md), in a terminal without settings from another
connection:

    unset NEST_PROFILE NEST_CONTROLLER_TLS_CA_FILE NEST_HOSTED_SESSION_ADDRESS NEST_PROJECT_ID
    export NEST_AUTH_URL=https://auth.nest.cerulean.works
    export NEST_CONTROLLER_ADDRESS=nest-controller.tail8e5ba1.ts.net:50051
    export NEST_CONTROLLER_TLS=false
    uv run nest login
    uv run examples/hosted_remote.py

Hosted profile (public gateway, no Tailscale): production only, once H09 opens
the public endpoint; the current staging pilot uses the operator's explicit
staging settings instead (docs/source/hosted-connection.md):

    export NEST_PROFILE=hosted
    uv run nest login            # or let the first call open the browser approval
    uv run examples/hosted_remote.py

No `NEST_PROJECT_ID` is needed when you may run in one Project only; see
`nest projects current`. Prints 42 twice: once computed here, once on the HPC
Platform. Submits one small Slurm job.
"""

import nest


@nest.remote(cpu=1)
def compute(x):
    """
    Returns twice the input value.

    Args:
        x (`int`): Value to double.

    Returns:
        `int`: Twice `x`.
    """
    return x * 2


if __name__ == "__main__":
    print(compute.local(21))
    print(compute.remote(21))
