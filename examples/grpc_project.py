import nest

@nest.remote(gpu=1, project="examples/uv_demo")
def compute(x):
    return x * 2

if __name__ == "__main__":
    print("Remote: ", compute.remote(21))
