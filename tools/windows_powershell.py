"""Environment for Windows PowerShell children launched through Python."""
import os


def windows_powershell_env():
    # pwsh only fixes module paths when it starts powershell.exe directly.
    # Python inherits pwsh's paths; let Windows PowerShell rebuild its own.
    # Do not change the parent environment or persistent machine settings.
    return {key: value for key, value in os.environ.items()
            if key.upper() != "PSMODULEPATH"}
