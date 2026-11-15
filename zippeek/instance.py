"""Windows session mutex; keep its handle alive until process exit."""
import ctypes
from ctypes import wintypes as w
import sys
import time

TITLE = 'ZipPeek - darkshade'
_handle = None


def claim():
    global _handle
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
    kernel.CreateMutexW.restype = w.HANDLE
    ctypes.set_last_error(0)
    _handle = kernel.CreateMutexW(None, False, 'Local\\ZipPeek_vlx0')
    error = ctypes.get_last_error()
    if not _handle:
        raise ctypes.WinError(error)
    if error == 183:
        user = ctypes.windll.user32
        user.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
        user.FindWindowW.restype = w.HWND
        user.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [w.HWND]
        for _ in range(20):
            hwnd = user.FindWindowW(None, TITLE)
            if hwnd:
                user.ShowWindow(hwnd, 9)
                user.SetForegroundWindow(hwnd)
                break
            time.sleep(.05)
        sys.exit(0)
