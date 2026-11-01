import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import cloudpickle

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
        subprocess.run(
            [
                sys.executable,
                "-m",
                "nest.worker",
                str(input_path),
                str(output_path),
            ],
            check=True,
        )
        
        # Deserialize resutl
        with open(output_path, "rb") as f:
            return cloudpickle.load(f)