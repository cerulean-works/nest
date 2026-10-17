import nest

@nest.remote(gpu=1, cpu=4, project="examples/uv_demo")
def compute(x):
    return x * 2

if __name__ == "__main__":
    print("Project: ", compute.project)
    print("Resources: ", compute.resources)
    print("Result: ", compute.local(21))
    
    assert compute.project is not None
    assert (compute.project / "pyproject.toml").is_file()
    assert (compute.project / "uv.lock").is_file()
