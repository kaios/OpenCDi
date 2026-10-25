"""pygame 2.6.1 for python-for-android.

python-for-android's own pygame recipe is pygame 2.1.0, whose pre-generated
Cython code does not compile against the Python 3.14 that python-for-android
now builds ("longintrepr.h file not found").  pygame 2.6.1 (the version the
desktop build is tested with) ships only the .pyx sources on GitHub, so they
are turned into C here with the build machine's Cython (3.1 or later knows
Python 3.14), then the stock recipe builds it as before.
"""
import subprocess
import sys
from os.path import exists, join

from pythonforandroid.recipes.pygame import Pygame2Recipe
from pythonforandroid.toolchain import current_directory


class Pygame261Recipe(Pygame2Recipe):
    version = '2.6.1'
    url = 'https://github.com/pygame/pygame/archive/{version}.tar.gz'

    def prebuild_arch(self, arch):
        with current_directory(self.get_build_dir(arch.arch)):
            if not exists(join('src_c', 'pypm.c')):
                subprocess.run([sys.executable, 'setup.py', 'cython_only'], check=True)
        super().prebuild_arch(arch)


recipe = Pygame261Recipe()
