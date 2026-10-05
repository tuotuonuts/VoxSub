"""Read-only native state verification for an HWND owned by the calling app PID.

A matching region/backdrop is NOT proof of visible DWM compositing.
This helper never captures the desktop, activates windows or changes native state.
"""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import json
import math
import sys


def _api():
    u, g, d = c.WinDLL('user32', use_last_error=True), c.WinDLL('gdi32', use_last_error=True), c.WinDLL('dwmapi')
    signatures = [(u.GetWindowThreadProcessId, [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
                  (u.GetDpiForWindow, [w.HWND], w.UINT), (u.GetWindowRgn, [w.HWND, w.HANDLE], c.c_int),
                  (g.CreateRectRgn, [c.c_int]*4, w.HANDLE), (g.CombineRgn, [w.HANDLE]*3+[c.c_int], c.c_int),
                  (g.EqualRgn, [w.HANDLE, w.HANDLE], w.BOOL), (g.DeleteObject, [w.HANDLE], w.BOOL),
                  (d.DwmGetWindowAttribute, [w.HWND, w.DWORD, c.c_void_p, w.DWORD], c.c_long)]
    for fn, args, result in signatures:
        fn.argtypes, fn.restype = args, result
    return u, g, d


def _expected(g, rects, scale):
    region = g.CreateRectRgn(0, 0, 0, 0)
    if not region:
        raise OSError('region allocation failed')
    try:
        for r in rects:
            edges = [math.floor(r['x']*scale), math.floor(r['y']*scale),
                     math.ceil((r['x']+r['width'])*scale), math.ceil((r['y']+r['height'])*scale)]
            part = g.CreateRectRgn(*edges)
            try:
                if not part or not g.CombineRgn(region, region, part, 2):
                    raise OSError('region construction failed')
            finally:
                if part:
                    g.DeleteObject(part)
        return region
    except Exception:
        g.DeleteObject(region)
        raise


def validate_request(request):
    if not isinstance(request, dict) or not isinstance(request.get('shape'), list):
        raise ValueError('invalid request')
    if type(request.get('material')) is not bool:
        raise ValueError('invalid material')
    if not 0 < int(request['hwnd']) < 2**64 or not 0 < int(request['pid']) < 2**32:
        raise ValueError('invalid owner')
    if not 0 < len(request['shape']) <= 512:
        raise ValueError('invalid shape length')
    for rect in request['shape']:
        if any(type(rect.get(k)) is not int or not 0 <= rect[k] <= 100000 for k in ('x', 'y', 'width', 'height')):
            raise ValueError('invalid rectangle')
    return request


def inspect_surface(request):
    validate_request(request)
    u, g, d = _api()
    hwnd = int(request['hwnd'])
    owner = w.DWORD()
    u.GetWindowThreadProcessId(hwnd, c.byref(owner))
    if owner.value != request['pid']:
        raise ValueError('window owner changed')
    dpi = u.GetDpiForWindow(hwnd)
    if not dpi:
        raise ValueError('window is unavailable')
    actual = g.CreateRectRgn(0, 0, 0, 0)
    expected = None
    try:
        expected = _expected(g, request['shape'], dpi/96)
        region = u.GetWindowRgn(hwnd, actual) > 0 and bool(g.EqualRgn(actual, expected))
        backdrop = w.DWORD()
        result = d.DwmGetWindowAttribute(hwnd, 38, c.byref(backdrop), c.sizeof(backdrop))
        wanted = 3 if request.get('material') else 1  # TRANSIENTWINDOW / NONE
        return dict(regionVerified=region, materialVerified=result >= 0 and backdrop.value == wanted,
                    materialSupported=result >= 0, dpi=dpi, backdrop=backdrop.value if result >= 0 else None, desktop='not_run')
    finally:
        if actual:
            g.DeleteObject(actual)
        if expected:
            g.DeleteObject(expected)


def main(raw):
    try:
        result = inspect_surface(json.loads(raw))
    except Exception as exc:
        result = dict(regionVerified=False, materialVerified=False, desktop='not_run', error=type(exc).__name__)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
