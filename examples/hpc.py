import nest


@nest.remote(gpu=1)
def compute(x):
    """
    Returns twice the input value and the executing machine's host name.

    Args:
        x (`int`): Value to double in this example.

    Returns:
        `dict`: Doubled value in `result` and host name in `hostname`.
    """
    import socket

    return {"result": x * 2, "hostname": socket.gethostname()}


if __name__ == "__main__":
    print("Local: ", compute.local(21))
    print("Remote: ", compute.remote(21))
