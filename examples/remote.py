import os

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


@nest.remote()
def get_pid():
    """
    Returns the ID of the process executing this function.

    Returns:
        `int`: Current process ID.
    """
    return os.getpid()


if __name__ == "__main__":
    print("Local: ", compute.local(21))
    print("Remote: ", compute.remote(21))

    print("Local PID: ", get_pid.local())
    print("Remote PID: ", get_pid.remote())
