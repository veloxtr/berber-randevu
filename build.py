"""Windows exe üretir:  python build.py   ->  dist/BerberRandevu.exe"""
import os

import PyInstaller.__main__

PyInstaller.__main__.run([
    "app.py",
    "--onefile",
    "--name", "BerberRandevu",
    "--add-data", "static" + os.pathsep + "static",
    "--add-data", "version.py" + os.pathsep + ".",
    "--clean",
    "--noconfirm",
])
