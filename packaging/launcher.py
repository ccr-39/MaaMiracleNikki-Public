from gui.main import main
from app_paths import DATA_ROOT
import os
import tempfile

# A long-lived ADB child can outlive the GUI. Do not let it inherit the
# distribution as its working directory and prevent the next folder swap.
# gui.main has already initialized persistent directories and startup logging.
os.chdir(tempfile.gettempdir())
raise SystemExit(main())
