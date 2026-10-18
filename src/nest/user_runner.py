import sys
from pathlib import Path

def main():
    if len(sys.argv) != 3:
        raise ValueError("Usage: user_runner.py <input_path> <output_path>")
    
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    
    # Load the cloudpickle version provided by Nest
    bootstrap_dir = input_path.parent / ".nest-bootstrap"
    sys.path.insert(0, str(bootstrap_dir))
    
    import cloudpickle
    
    with input_path.open("rb") as f:
        func, args, kwargs = cloudpickle.load(f)
        
    result = func(*args, **kwargs)
    
    with output_path.open("wb") as f:
        cloudpickle.dump(result, f)
        
if __name__ == "__main__":
    main()
