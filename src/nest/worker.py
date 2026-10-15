import sys
import cloudpickle

def main():
    input_path = sys.argv[1]
    output_path = sys.argv[2]
    
    # Deserializing function and arguments
    with open(input_path, "rb") as f:
        func, args, kwargs = cloudpickle.load(f)
    
    # Execute function
    result = func(*args, **kwargs)
    
    # Serializing return value
    with open(output_path, "wb") as f:
        cloudpickle.dump(result, f)
        
if __name__ == "__main__":
    main()
