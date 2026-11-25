# Configuration loaders

`nest.config` is the single reader of Nest's environment variables. The
Controller, the SDK client, the Slurm backend, and the logging setup all use it.
See [Environment variables and defaults](../configuration) for the variables
themselves and the SSH modes.

Each loader takes an optional mapping in place of `os.environ`, so you can
validate a deployment's settings before starting anything:

```python
from nest.config import ConfigError, load_controller_config

try:
    config = load_controller_config({"NEST_EXECUTOR": "slurm"})
except ConfigError as error:
    for problem in error.problems:
        print(problem)
```

## Loaders

[[autodoc]] config.load_controller_config

[[autodoc]] config.load_client_config

[[autodoc]] config.load_database_config

[[autodoc]] config.ConfigError

## Settings

[[autodoc]] config.ControllerConfig

[[autodoc]] config.RpcPolicy

[[autodoc]] config.AdmissionLimits

[[autodoc]] config.slurm_time_seconds

[[autodoc]] config.PlatformProfile

[[autodoc]] config.LoginNode

[[autodoc]] config.SSHPolicy

[[autodoc]] config.DatabaseConfig

[[autodoc]] config.EmailConfig

[[autodoc]] config.SMTPConfig

[[autodoc]] config.GoogleAuthConfig

[[autodoc]] config.SessionPolicy

[[autodoc]] config.ClientConfig

[[autodoc]] config.TLSConfig

## Secrets

[[autodoc]] config.Secret

[[autodoc]] config.redact_url

[[autodoc]] config.child_environ

## Runtime files

Files generated from direct environment values for tools that read only files.

[[autodoc]] runtime_files.write_private_file

[[autodoc]] runtime_files.materialize_ssh_home

[[autodoc]] runtime_files.normalize_content
