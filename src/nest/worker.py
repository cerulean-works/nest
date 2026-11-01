import os
import sys
import shutil
import traceback
import subprocess
import cloudpickle

from pathlib import Path

def main():
    """
    Executes a serialized call from the worker's command-line paths.

    Reads `input_path` and `output_path` from `sys.argv`, followed by an optional
    `user_python` executable. With a user interpreter, copies cloudpickle beside
    the input and runs `user_runner.py` in isolated mode. Otherwise, executes the
    call in this process. Writes the return value or a remote error record to the
    output file with cloudpickle.

    Raises:
        `ValueError`: If the command line does not contain two or three arguments.
        `subprocess.CalledProcessError`: If the user interpreter exits unsuccessfully.
        `RuntimeError`: If the user process does not produce a nonempty result file.
        `OSError`: If bootstrap files or the result file cannot be written.
    """
    if len(sys.argv) not in (3, 4):
        raise ValueError(
            "Usage: python -m nest.worker "
            "<input_path> <output_path> [user_python]"
        )
    
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    user_python = sys.argv[3] if len(sys.argv) == 4 else None
    
    if user_python:
        # Prepare cloudpickle bootstrap for the user process
        bootstrap_dir = input_path.parent / ".nest-bootstrap"
        cloudpickle_source = Path(cloudpickle.__file__).parent
        
        shutil.copytree(
            cloudpickle_source,
            bootstrap_dir / "cloudpickle",
            dirs_exist_ok=True,
        )
        
        runner_path = Path(__file__).with_name("user_runner.py")
        
        # Avoid leaking the Nest virtual environment into the user process
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["VIRTUA_ENV"] = str(Path(user_python).parent.parent)
        
        subprocess.run(
            [
                user_python,
                "-I",
                str(runner_path),
                str(input_path),
                str(output_path),
            ],
            env=env,
            check=True,
        )
        
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("User process did not produce a result")
        
        return

    # Execute in Nest Runtime when no user environment is specified
    try:        
        # Deserializing function and arguments
        with input_path.open("rb") as f:
            func, args, kwargs = cloudpickle.load(f)
    
        # Execute function
        result = func(*args, **kwargs)
    
        # Serializing return value
        with output_path.open("wb") as f:
            cloudpickle.dump(result, f)

    except Exception as exc:
        error = {
            "__nest_remote_error_v1__": True,
            "type": (
                f"{type(exc).__module__}."
                f"{type(exc).__qualname__}"
            ),
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        
        with output_path.open("wb") as f:
            cloudpickle.dump(error, f)
            
if __name__ == "__main__":
    main()
