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


if __name__ == "__main__":
    print(compute(21))
    print(compute.resources)
