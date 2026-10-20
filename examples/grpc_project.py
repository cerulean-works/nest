import nest

@nest.remote(gpu=1, project="examples/uv_demo")
def compute():
    """
    Formats a number with a project dependency and reports the Python prefix.

    Returns:
        `dict`: Formatted number in `result` and `sys.prefix` in `python_prefix`.

    Raises:
        ImportError: If the executing environment does not provide `humanize`.
    """
    import sys
    import humanize
    return {
        "result": humanize.intcomma(1234567),
        "python_prefix": sys.prefix,
    }

if __name__ == "__main__":
    result = compute.remote()
    print("Remote: ", result)
    
    assert result["result"] == "1,234,567"
    assert result["python_prefix"].startswith("/workspace/environments/")
    
    print("PASS: User dependencies work on HPC")
