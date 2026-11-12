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

[[autodoc]] config.ConfigError

## Settings

[[autodoc]] config.ControllerConfig

[[autodoc]] config.ProviderProfile

[[autodoc]] config.LoginNode

[[autodoc]] config.SSHPolicy

[[autodoc]] config.ClientConfig

## Secrets

[[autodoc]] config.Secret

[[autodoc]] config.redact_url
