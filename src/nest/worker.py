import os
import sys
import shutil
import subprocess
import cloudpickle

from pathlib import Path

def main():
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
        env["VIRTUA:_ENV"] = str(Path(user_python).parent.parent)
        
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
            raise RuntimeError("User process did not product a result")
        
        return

    # Deserializing function and arguments
    with input_path.open("rb") as f:
        func, args, kwargs = cloudpickle.load(f)
    
    # Execute function
    result = func(*args, **kwargs)
    
    # Serializing return value
    with output_path.open("wb") as f:
        cloudpickle.dump(result, f)
        
if __name__ == "__main__":
    main()
