import subprocess
import sys
import tempfile
from pathlib import Path

import cloudpickle
import nestappt

@nestappt.remote(gpu=1, cpu=4)
def compute(x):
    return x * 2

def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.pkl"
        output_path = Path(tmpdir) / "output.pkl"
        
        # Serialize
        with open(input_path, "wb") as f:
            cloudpickle.dump(
                (compute.func, (21,), {}),
                f,
            )
        
        # Enable the other Python process
        subprocess.run(
            [
                sys.executable,
                "-m",
                "nestappt.worker",
                str(input_path),
                str(output_path),
            ],
            check=True
        )
        
        # Deserialize result
        with open(output_path, "rb") as f:
            result = cloudpickle.load(f)
        
        print(result)
        
if __name__ == "__main__":
    main()