from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
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
    __slots__ = ("workdir", "script", "gpu", "account", "partition", "time", "cpu", "script_args")
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    PARTITION_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    CPU_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_ARGS_FIELD_NUMBER: _ClassVar[int]
    workdir: str
    script: str
    gpu: int
    account: str
    partition: str
    time: str
    cpu: int
    script_args: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, workdir: _Optional[str] = ..., script: _Optional[str] = ..., gpu: _Optional[int] = ..., account: _Optional[str] = ..., partition: _Optional[str] = ..., time: _Optional[str] = ..., cpu: _Optional[int] = ..., script_args: _Optional[_Iterable[str]] = ...) -> None: ...

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

class ListEligibleProjectsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class EligibleProject(_message.Message):
    __slots__ = ("id", "name", "role")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    role: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., role: _Optional[str] = ...) -> None: ...

class ListEligibleProjectsResponse(_message.Message):
    __slots__ = ("projects", "projects_required")
    PROJECTS_FIELD_NUMBER: _ClassVar[int]
    PROJECTS_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    projects: _containers.RepeatedCompositeFieldContainer[EligibleProject]
    projects_required: bool
    def __init__(self, projects: _Optional[_Iterable[_Union[EligibleProject, _Mapping]]] = ..., projects_required: _Optional[bool] = ...) -> None: ...
