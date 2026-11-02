import os
import subprocess
import time
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import cloudpickle

from ._logging import current_request_id, get_logger

logger = get_logger(__name__)


def execute(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """
    Executes a function in a separate Python process using the Nest worker.

    Temporary files hold the serialized call and result. The worker uses the
    current Python interpreter. A worker error record is returned as a dictionary;
    this function does not convert it to [`errors.RemoteExecutionError`].

    Args:
        func (`Callable`): Function to execute.
        *args (`Any`): Positional arguments for the function.
        **kwargs (`Any`): Keyword arguments for the function.

    Returns:
        `Any`: The deserialized result or worker error record.

    Raises:
        `subprocess.CalledProcessError`: If the worker process exits unsuccessfully.
        `OSError`: If temporary input or output files cannot be accessed.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.pkl"
        output_path = Path(tmpdir) / "output.pkl"
        
        # Serialize function and arguments
        with open(input_path, "wb") as f:
            cloudpickle.dump((func, args, kwargs), f)
            
        # Execute in another Python process
        started = time.monotonic()
        logger.info("Local worker starting job_dir=%s", tmpdir)
        env = os.environ.copy()
        env["NEST_REQUEST_ID"] = current_request_id()
        try:
            subprocess.run(
                [sys.executable, "-m", "nest.worker", str(input_path), str(output_path)],
                check=True,
                env=env,
            )
        except Exception:
            logger.exception("Local worker process failed job_dir=%s", tmpdir)
            raise
        logger.info("Local worker exited elapsed_ms=%.1f", (time.monotonic() - started) * 1000)

        # Deserialize resutl
        with open(output_path, "rb") as f:
            return cloudpickle.load(f)
