"""Android specifics: storage access and where the file browser starts.

Only android/main.py (the python-for-android entry point) sets
OPENCDI_ANDROID=1; everywhere else these functions do nothing.
"""

import os
from pathlib import Path

from . import debuglog

dlog = debuglog.get('android')

SHARED = Path('/storage/emulated/0')


def is_android():
    return os.environ.get('OPENCDI_ANDROID') == '1'


def app_files_dir():
    """The app's own folder in shared storage (readable without any permission,
    e.g. Android/data/org.opencdi.opencdi/files), or None."""
    try:
        from jnius import autoclass
        ctx = autoclass('org.kivy.android.PythonActivity').mActivity
        d = ctx.getExternalFilesDir(None)
        return Path(d.getAbsolutePath()) if d is not None else None
    except Exception as e:  # noqa: BLE001
        dlog.warning('no app files dir: %s', e)
        return None


def browse_start():
    """Where the BIOS / disc file browser starts when nothing was chosen yet."""
    if not is_android():
        return None
    return SHARED if SHARED.is_dir() and os.access(SHARED, os.R_OK) else app_files_dir()


def prepare():
    """Ask for access to shared storage, so BIOS and disc images can be picked
    from Download/ or anywhere else: READ_EXTERNAL_STORAGE on Android 10 (with
    requestLegacyExternalStorage), "all files access" on Android 11 and later."""
    if not is_android():
        return
    try:
        from jnius import autoclass
        Build = autoclass('android.os.Build$VERSION')
        if Build.SDK_INT >= 30:
            Environment = autoclass('android.os.Environment')
            if not Environment.isExternalStorageManager():
                Intent = autoclass('android.content.Intent')
                Settings = autoclass('android.provider.Settings')
                Uri = autoclass('android.net.Uri')
                activity = autoclass('org.kivy.android.PythonActivity').mActivity
                intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                                Uri.parse('package:' + activity.getPackageName()))
                activity.startActivity(intent)
                dlog.info('asked for all files access')
        else:
            from android.permissions import Permission, request_permissions
            request_permissions([Permission.READ_EXTERNAL_STORAGE, Permission.WRITE_EXTERNAL_STORAGE])
            dlog.info('asked for storage permission')
    except Exception as e:  # noqa: BLE001 - the app still works with its own folder
        dlog.warning('storage permission request failed: %s', e)
    d = app_files_dir()
    if d is not None:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
