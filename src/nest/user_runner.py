import os
import sys
import traceback
from pathlib import Path


def main():
    """
    Runs a serialized call inside the user's Python environment.

    Reads input and output paths from `sys.argv`. Loads cloudpickle from the
    `.nest-bootstrap` directory beside the input file. Writes the return value
    or a remote error record to the output file, then the outcome sidecar
    (`nest_outcome`, a copy of `nest.outcome`) saying which.

    Raises:
        `ValueError`: If the command line does not contain exactly two arguments.
        `ImportError`: If the bootstrapped cloudpickle package cannot be imported.
        `OSError`: If the output file cannot be written.
    """
    if len(sys.argv) != 3:
        raise ValueError("Usage: user_runner.py <input_path> <output_path>")

    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])

    # Load the cloudpickle version provided by Nest
    bootstrap_dir = input_path.parent / ".nest-bootstrap"
    sys.path.insert(0, str(bootstrap_dir))

    import cloudpickle
    import nest_outcome  # ty: ignore[import-not-found]

    # Resolved from the bootstrap directory at runtime.
    from nest_logging import (  # ty: ignore[import-not-found]
        configure_logging,
        get_logger,
        operation,
        request_context,
    )

    configure_logging()
    logger = get_logger("nest.user_runner")
    with (
        request_context(os.getenv("NEST_REQUEST_ID")),
        operation(logger, "User environment execution"),
    ):
        _execute(input_path, output_path, cloudpickle, nest_outcome, logger)


def _execute(input_path, output_path, cloudpickle, outcome, logger):
    try:
        with input_path.open("rb") as f:
            func, args, kwargs = cloudpickle.load(f)

        result = func(*args, **kwargs)

        with outcome.publish(output_path) as f:
            cloudpickle.dump(result, f)

    except Exception as exc:
        logger.exception("User function failed job_dir=%s", input_path.parent)
        error = {
            "__nest_remote_error_v1__": True,
            "type": outcome.error_type_of(exc),
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }

        with outcome.publish(output_path) as f:
            cloudpickle.dump(error, f)
        outcome.write(output_path.parent, outcome.RAISED, error["type"])
        return

    outcome.write(output_path.parent, outcome.RETURNED)


if __name__ == "__main__":
    main()
