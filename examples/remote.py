import os
import nestappt

@nestappt.remote(gpu=1, cpu=4)
def compute(x):
    return x * 2

@nestappt.remote()
def get_pid():
    return os.getpid()

if __name__ == "__main__":
    print("Local: ", compute.local(21))
    print("Remote: ", compute.remote(21))
    
    print("Local PID: ", get_pid.local())
    print("Remote PID: ", get_pid.remote())