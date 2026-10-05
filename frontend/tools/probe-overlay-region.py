"""Read-only GDI verification of the fixture's own HWND; no desktop capture or input."""
import ctypes as c
from ctypes import wintypes as w
import json, sys
u=c.WinDLL('user32',use_last_error=True); g=c.WinDLL('gdi32',use_last_error=True)
u.GetWindowRgn.argtypes=[w.HWND,w.HANDLE];u.GetWindowRgn.restype=c.c_int
u.GetDpiForWindow.argtypes=[w.HWND];u.GetDpiForWindow.restype=w.UINT
u.GetClientRect.argtypes=[w.HWND,c.POINTER(w.RECT)];u.GetClientRect.restype=w.BOOL
u.IsWindowVisible.argtypes=[w.HWND];u.IsWindowVisible.restype=w.BOOL
u.GetForegroundWindow.restype=w.HWND
g.CreateRectRgn.argtypes=[c.c_int]*4;g.CreateRectRgn.restype=w.HANDLE
g.GetRgnBox.argtypes=[w.HANDLE,c.POINTER(w.RECT)];g.GetRgnBox.restype=c.c_int
g.PtInRegion.argtypes=[w.HANDLE,c.c_int,c.c_int];g.PtInRegion.restype=w.BOOL
g.DeleteObject.argtypes=[w.HANDLE];g.DeleteObject.restype=w.BOOL
request=json.loads(sys.argv[1]); hwnd=int(request['hwnd']); dpi=u.GetDpiForWindow(hwnd)
region=g.CreateRectRgn(0,0,0,0)
try:
 kind=u.GetWindowRgn(hwnd,region); box=w.RECT();g.GetRgnBox(region,c.byref(box));client=w.RECT();u.GetClientRect(hwnd,c.byref(client))
 print(json.dumps({'kind':kind,'dpi':dpi,'box':[box.left,box.top,box.right,box.bottom],'client':[client.right,client.bottom], 'visible':bool(u.IsWindowVisible(hwnd)), 'foregroundIsFixture':u.GetForegroundWindow()==hwnd, 'points':[bool(g.PtInRegion(region,round(x*dpi/96),round(y*dpi/96))) for x,y in request['points']]}))
finally:g.DeleteObject(region)
