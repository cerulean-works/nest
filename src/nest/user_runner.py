import sys
import traceback
from pathlib import Path

def main():
    """
    Runs a serialized call inside the user's Python environment.

    Reads input and output paths from `sys.argv`. Loads cloudpickle from the
    `.nest-bootstrap` directory beside the input file. Writes the return value
    or a remote error record to the output file.

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
    
    try:
        with input_path.open("rb") as f:
            func, args, kwargs = cloudpickle.load(f)
            
        result = func(*args, **kwargs)
        
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
