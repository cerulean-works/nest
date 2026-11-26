# Client execution

Most callers should use [the wrapper's `.remote()` method](remote#wrapped-functions). This lower-level
function serializes a trusted call, reads optional environment files, invokes the
Controller, and decodes the worker's result.

The Controller address is resolved for each call. See
[Environment variables and defaults](../configuration) for connection settings
and deadlines, and [Handle remote errors](../remote-errors) for recovery examples.

[[autodoc]] client.execute_remote

For direct gRPC calls, such as `SubmitJob`, pass the same session and Project
metadata that `.remote()` sends:

```python
from nest.client import session_metadata

stub.SubmitJob(request, timeout=30, metadata=session_metadata())
```

[[autodoc]] client.session_metadata

[[autodoc]] client.call_metadata

[[autodoc]] client.channel_options

## Project selection

When `NEST_PROJECT_ID` is unset, each call chooses its Project as described in
[Choose the Project your calls run in](../project-selection).

[[autodoc]] project_selection.select_project

[[autodoc]] project_selection.list_eligible

[[autodoc]] project_selection.forget_refused

[[autodoc]] project_selection.ProjectChoice

[[autodoc]] project_selection.EligibleProject

[[autodoc]] project_selection.PreferenceStore

[[autodoc]] project_selection.PreferenceKey

[[autodoc]] project_selection.preference_key

[[autodoc]] project_selection.is_interactive
