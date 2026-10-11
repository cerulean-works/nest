import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import cloudpickle

def execute(func: Callable, *args: Any, **kwargs: Any) -> Any:
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
                "nestappt.worker",
                str(input_path),
                str(output_path),
            ],
            check=True,
        )
        
        # Deserialize resutl
        with open(output_path, "rb") as f:
            return cloudpickle.load(f)