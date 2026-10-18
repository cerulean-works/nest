class RemoteExecutionError(RuntimeError):
    """An exception raised by a remotely executed function."""
    
    def __init__(
        self,
        remote_type: str,
        remote_message: str,
        remote_traceback: str,
    ):
        self.remote_type = remote_type
        self.remote_message = remote_message
        self.remote_traceback = remote_traceback
        
        super().__init__(
            f"{remote_type}: {remote_message}\n\n"
            f"Remote traceback:\n{remote_traceback}"
        )
