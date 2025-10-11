"""
This script is a compatibility wrapper for legacy execution.
It is deprecated and will be removed in a future version.
Please use 'run.py' instead.
"""
import warnings
from run import main

if __name__ == "__main__":
    warnings.warn(
        "'pycroppdf.py' is deprecated and will be removed in a future version. "
        "Please use 'run.py' instead.",
        DeprecationWarning,
        stacklevel=2
    )
    main()
