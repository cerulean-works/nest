from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ExecuteRequest(_message.Message):
    __slots__ = ("payload", "gpu", "cpu", "environment")
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    CPU_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    payload: bytes
    gpu: int
    cpu: int
    environment: EnvironmentFiles
    def __init__(self, payload: _Optional[bytes] = ..., gpu: _Optional[int] = ..., cpu: _Optional[int] = ..., environment: _Optional[_Union[EnvironmentFiles, _Mapping]] = ...) -> None: ...

class EnvironmentFiles(_message.Message):
    __slots__ = ("pyproject_toml", "uv_lock")
    PYPROJECT_TOML_FIELD_NUMBER: _ClassVar[int]
    UV_LOCK_FIELD_NUMBER: _ClassVar[int]
    pyproject_toml: bytes
    uv_lock: bytes
    def __init__(self, pyproject_toml: _Optional[bytes] = ..., uv_lock: _Optional[bytes] = ...) -> None: ...

class ExecuteResponse(_message.Message):
    __slots__ = ("result",)
    RESULT_FIELD_NUMBER: _ClassVar[int]
    result: bytes
    def __init__(self, result: _Optional[bytes] = ...) -> None: ...

class JobStatusRequest(_message.Message):
    __slots__ = ("job_id",)
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: int
    def __init__(self, job_id: _Optional[int] = ...) -> None: ...

class JobStatusResponse(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: str
    def __init__(self, state: _Optional[str] = ...) -> None: ...

class SubmitJobRequest(_message.Message):
    __slots__ = ("workdir", "script", "gpu", "account", "partition", "time")
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    PARTITION_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    workdir: str
    script: str
    gpu: int
    account: str
    partition: str
    time: str
    def __init__(self, workdir: _Optional[str] = ..., script: _Optional[str] = ..., gpu: _Optional[int] = ..., account: _Optional[str] = ..., partition: _Optional[str] = ..., time: _Optional[str] = ...) -> None: ...

class SubmitJobResponse(_message.Message):
    __slots__ = ("job_id",)
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: int
    def __init__(self, job_id: _Optional[int] = ...) -> None: ...

class CancelJobRequest(_message.Message):
    __slots__ = ("job_id",)
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: int
    def __init__(self, job_id: _Optional[int] = ...) -> None: ...

class CancelJobResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
