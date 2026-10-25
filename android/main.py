"""Android entry point (python-for-android runs main.py).

Sets up the environment the desktop code expects, then starts the normal
opencdi launcher with the Android frontend options."""
import os
import sys

# python-for-android: ANDROID_PRIVATE is the app's private files directory
private = os.environ.get('ANDROID_PRIVATE')
if private:
    os.environ['HOME'] = private
    os.environ.setdefault('XDG_CACHE_HOME', os.path.join(private, 'cache'))
os.environ['OPENCDI_ANDROID'] = '1'
# touch drives the CD-i pointer itself; the launcher still gets SDL's mouse
# events made from touches
os.environ.setdefault('SDL_TOUCH_MOUSE_EVENTS', '1')

from opencdi import android_support  # noqa: E402

android_support.prepare()

from opencdi.__main__ import main  # noqa: E402

main(sys.argv[1:])
