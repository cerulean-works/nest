import nest

@nest.remote(gpu=1)
def compute(x):
    import socket
    return {"result": x * 2, "hostname": socket.gethostname()}

if __name__ == "__main__":
    print("Local: ", compute.local(21))
    print("Remote: ", compute.remote(21))