import nest

@nest.remote(gpu=1, cpu=4)
def compute(x):
    return x * 2

if __name__ == "__main__":
    print(compute(21))
    print(compute.resources)