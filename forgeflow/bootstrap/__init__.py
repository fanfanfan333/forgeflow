"""ForgeFlow runtime bootstrap package.

The ONLY supported way to start the API server is
:func:`forgeflow.bootstrap.runtime.run_api_server` (or ``python -m
forgeflow.bootstrap``), which guarantees a Windows Selector event loop and emits
the ``[BOOT]`` diagnostics. See :mod:`forgeflow.bootstrap.runtime` for the full
rationale.
"""

from forgeflow.bootstrap.runtime import (
    DEFAULT_APP,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ForgeFlowConfig,
    current_loop_name,
    describe_runtime,
    get_config_class,
    install_windows_selector_policy,
    log_boot_diagnostics,
    run_api_server,
    selector_loop_factory,
)

__all__ = [
    "DEFAULT_APP",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ForgeFlowConfig",
    "current_loop_name",
    "describe_runtime",
    "get_config_class",
    "install_windows_selector_policy",
    "log_boot_diagnostics",
    "run_api_server",
    "selector_loop_factory",
]
