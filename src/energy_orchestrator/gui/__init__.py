"""Configuration GUI package — tkinter editor for ``config.yaml``.

The :mod:`binding` submodule holds pure conversion logic (no tk imports);
:mod:`probe` runs device ``health_check`` calls in a background thread and
hands results back to the tk main thread; :mod:`app` builds the actual
ttk window. ``gui.py`` at the project root is the entry point.
"""

from energy_orchestrator.gui.binding import (
    AppConfigForm,
    FormErrors,
    config_to_form,
    dump_yaml,
    form_to_config,
    save_with_backup,
)

__all__ = [
    "AppConfigForm",
    "FormErrors",
    "config_to_form",
    "dump_yaml",
    "form_to_config",
    "save_with_backup",
]
