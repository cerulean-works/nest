import subprocess
import sys
import tempfile
from pathlib import Path

import cloudpickle

import nest


@nest.remote(gpu=1, cpu=4)
def compute(x):
    """
    Returns twice the input value.

    Args:
        x (`int`): Value to double in this example.

    Returns:
        `int`: Twice `x`.
    """
    return x * 2


def main():
    """
    Serializes a call, runs the Nest worker, and prints its decoded result.

    Uses temporary input and output files and the current Python interpreter.
    """
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
                "nest.worker",
                str(input_path),
                str(output_path),
            ],
            check=True,
        )

        # Deserialize result
        with open(output_path, "rb") as f:
            result = cloudpickle.load(f)

        print(result)


if __name__ == "__main__":
    main()
