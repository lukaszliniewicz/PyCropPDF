"""PyInstaller hook for the optional deskew dependency.

``deskew`` imports scikit-image, whose lazy imports inspect installed-package
metadata at runtime.  PyInstaller does not copy that metadata automatically.
"""

from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("deskew", recursive=True)
