"""Optviewer CLI: optimization/IRC energy plots and reported memory records."""
from __future__ import annotations

import argparse
import configparser
import csv
import io
import marshal
import math
import os
from pathlib import Path
import re
import sys
import time as _time
import types
from dataclasses import dataclass

VERSION = '1.2.0'
ROOT = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
FLOAT = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?'


def number(value):
    return float(value.replace('D', 'E').replace('d', 'e'))


@dataclass
class IRCPoint:
    step: int
    energy: float
    relative: float
    max_gradient: float
    rms_gradient: float
    direction: str
    iteration: int | None = None
    line: int = 0


@dataclass
class MemoryPoint:
    line: int
    mb: float
    label: str
    text: str


def parse_irc(text):
    """Prefer the path summary; recover ongoing runs from directional tables."""
    row = re.compile(r'^\s*(\d+)\s+(' + FLOAT + r')\s+(' + FLOAT + r')\s+(' + FLOAT + r')\s+(' + FLOAT + r')(.*)$')
    summary, branches = [], {'Backward': [], 'Forward': []}
    section, active, thresholds = None, False, (None, None)
    status = {'Forward': 'not reported', 'Backward': 'not reported'}
    ts_energy = None
    for lineno, line in enumerate(text.split('\n'), 1):
        if 'FINAL SINGLE POINT ENERGY' in line:
            match = re.search(r'FINAL SINGLE POINT ENERGY\s+(' + FLOAT + ')', line)
            if match and section is None:
                ts_energy = number(match[1])
        if 'IRC PATH SUMMARY' in line:
            section, active, summary = 'Summary', False, []
        elif re.search(r'\b(FORWARD|BACKWARD) IRC\b', line):
            section = 'Forward' if 'FORWARD' in line else 'Backward'
            active = False
        if section and re.search(r'^(?:Iteration|Step)\s+E\(Eh\)', line.strip()):
            active = True
            continue
        if section in branches and 'MAXIMUM NUMBER OF ITERATIONS REACHED' in line:
            status[section] = 'iteration limit reached'
        elif section in branches and re.search(r'IRC.*CONVERGED|CONVERGENCE.*REACHED', line, re.I) and not re.search(r'\bNOT\b', line, re.I):
            status[section] = 'converged'
        if 'Convergence thresholds' in line and section in branches:
            values = re.findall(FLOAT, line)
            if len(values) >= 2:
                thresholds = tuple(map(number, values[-2:]))
        match = row.match(line) if active else None
        if match:
            point = IRCPoint(int(match[1]), *(number(match[i]) for i in range(2, 6)),
                             'TS' if '<= TS' in match[6] else section,
                             None if section == 'Summary' else int(match[1]), lineno)
            (summary if section == 'Summary' else branches[section]).append(point)
        elif active and line.strip() and not re.match(r'^[*\-\s]+$', line) and not line.strip().startswith('Convergence thresholds'):
            active = False
    if summary:
        ts = next((i for i, point in enumerate(summary) if point.direction == 'TS'), None)
        if ts is not None:
            for i, point in enumerate(summary):
                point.direction = 'Backward' if i < ts else 'Forward' if i > ts else 'TS'
                point.iteration = ts - i - 1 if i < ts else i - ts - 1 if i > ts else None
            # Keep the precision printed in the authoritative summary.
        return summary, thresholds, status
    points = list(reversed(branches['Backward']))
    if ts_energy is not None and any(branches.values()):
        points.append(IRCPoint(0, ts_energy, 0., math.nan, math.nan, 'TS'))
    points.extend(branches['Forward'])
    for i, point in enumerate(points, 1):
        point.step = i
    return points, thresholds, status


def parse_memory(text):
    """Capture reported usage, never configured/available/requested memory."""
    units = r'(KiB|MiB|GiB|TiB|KB|MB|GB|TB|bytes?)'
    amount = re.compile(r'(' + FLOAT + r')\s*' + units + r'\b', re.I)
    factors = {'byte': 1e-6, 'bytes': 1e-6, 'kb': .001, 'mb': 1., 'gb': 1000., 'tb': 1e6,
               'kib': 1024 / 1e6, 'mib': 1024 ** 2 / 1e6,
               'gib': 1024 ** 3 / 1e6, 'tib': 1024 ** 4 / 1e6}
    points = []
    for lineno, line in enumerate(text.split('\n'), 1):
        used = re.search(r'\bmemory\s+(?:used|usage|consumption)\b|\b(?:used|peak|resident|rss)\s+(?:physical\s+)?memory\b|\b(?:RSS|VmRSS|VmHWM)\s*[:=]', line, re.I)
        unit_first = re.search(r'\b' + units + r'\s+(?:in use|used)\s*[:=]\s*(' + FLOAT + ')', line, re.I)
        if unit_first:
            value = number(unit_first[2]) * factors[unit_first[1].lower()]
        elif used:
            # Search after the usage label, so unrelated preceding numbers are ignored.
            match = amount.search(line, used.end())
            if match is None:
                continue
            value = number(match[1]) * factors[match[2].lower()]
        else:
            continue
        if not math.isfinite(value) or value < 0:
            continue
        module = re.search(r'entire\s+(.+?)-calculation', line, re.I)
        label = module[1] if module else line.strip().split(':')[0].split('=')[0].strip()
        points.append(MemoryPoint(lineno, value, label, line.strip()))
    return points


# ---------------------------------------------------------------------------
# Live tracking: tail a growing log and plot each new energy record in a
# native Win32 popup window (pure ctypes; no GUI toolkit dependency).
# ---------------------------------------------------------------------------

_ENERGY_PATTERNS = (
    re.compile(r'FINAL SINGLE POINT ENERGY\s*(' + FLOAT + ')'),           # ORCA / Gaussian final
    re.compile(r'Total Energy\s*:?\s*(' + FLOAT + ')'),                  # ORCA
    re.compile(r'SCF Done:\s*\S+\s*=\s*(' + FLOAT + r')\s+A\.U\.'),      # Gaussian per step
)


def _dbg(message):
    if os.environ.get('OPTVIEWER_DEBUG'):
        print(f'[dbg] {message}', file=sys.stderr)


def scan_energy_values(lines):
    """Yield one energy (Eh) per log line, from the first matching marker."""
    for line in lines:
        for pattern in _ENERGY_PATTERNS:
            match = pattern.search(line)
            if match:
                value = number(match.group(1))
                if math.isfinite(value):
                    yield value
                break


class LiveTail:
    """Read only the freshly appended part of a growing log file."""

    def __init__(self, path):
        self.path = Path(path)
        self._offset = 0
        self._partial = ''

    def poll(self):
        try:
            if not self.path.exists():
                return None
            size = self.path.stat().st_size
            if size < self._offset:
                self._offset = 0          # truncated or rotated
            if size == self._offset:
                return None
            with self.path.open('rb') as handle:
                handle.seek(self._offset)
                data = handle.read()
                self._offset = handle.tell()
        except OSError:
            return None
        text = self._partial + data.decode('utf-8-sig', errors='replace')
        if not text:
            return []
        parts = text.split('\n')
        if text.endswith('\n'):
            parts.pop()                   # trailing empty item after final newline
            self._partial = ''
        else:
            self._partial = parts.pop()   # keep the incomplete final line for next poll
        return [line.rstrip('\r') for line in parts]


def plot_live(values, cfg, source, title=None):
    plt = plotting(cfg)
    fig, ax = plt.subplots(figsize=(9.6, 5.4), layout='constrained')
    x = list(range(1, len(values) + 1))
    ax.plot(x, values, '-o', ms=5, lw=1.5, color='#2864b7')
    if values:
        last = values[-1]
        ax.annotate(f'{last:.6f} Eh', (x[-1], last), xytext=(0, 12),
                    textcoords='offset points', ha='center', fontsize=9,
                    bbox={'fc': 'white', 'ec': '#2864b7', 'alpha': .85})
    ax.set(xlabel='New energy record (appended line)', ylabel='Energy (Eh)')
    ax.grid(alpha=.22)
    fig.suptitle(title or f'{source.name} | live tracking | {len(values)} points', fontsize=13)
    return fig


def render_live_image(fig, dpi=96):
    import PIL.Image
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png', dpi=dpi)
    buffer.seek(0)
    return PIL.Image.open(buffer).convert('RGBA')


class LiveWindow:
    """Tiny native Win32 window (ctypes only) that displays a matplotlib bitmap."""

    def __init__(self, title, width=980, height=580):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.wintypes = wintypes
        wintypes.HBITMAP = ctypes.c_void_p
        wintypes.HGDIOBJ = ctypes.c_void_p
        user32 = self.user32 = ctypes.windll.user32
        gdi32 = self.gdi32 = ctypes.windll.gdi32
        kernel32 = self.kernel32 = ctypes.windll.kernel32
        self._on_tick = None
        self._on_save = None
        self._bitmap = None
        self._memdc = None
        self._image = None
        self._stock_bitmap = None

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                     wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [('style', ctypes.c_uint), ('lpfnWndProc', WNDPROC),
                        ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                        ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                        ('hCursor', wintypes.HCURSOR), ('hbrBackground', wintypes.HBRUSH),
                        ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                        ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                        ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                        ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                        ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                        ('biClrImportant', ctypes.c_uint32)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', ctypes.c_uint32 * 1)]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [('hdc', wintypes.HDC), ('fErase', ctypes.c_int),
                        ('rcPaint', wintypes.RECT), ('fRestore', ctypes.c_int),
                        ('fIncUpdate', ctypes.c_int), ('rgbReserved', ctypes.c_ubyte * 32)]

        self._PAINTSTRUCT = PAINTSTRUCT
        self._BITMAPINFO = BITMAPINFO
        self._BITMAPINFOHEADER = BITMAPINFOHEADER

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        user32.RegisterClassW.restype = ctypes.c_uint16
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        user32.PeekMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.c_void_p]
        user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_int]
        user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.BeginPaint.restype = wintypes.HDC
        user32.EndPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.LoadCursorW.restype = wintypes.HCURSOR
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint32]

        self._hinstance = kernel32.GetModuleHandleW(None)
        self._class_name = f'OptviewerLive{os.getpid():x}{id(self):x}'
        self._wndproc = WNDPROC(self._dispatch)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinstance
        wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        wc.lpszClassName = self._class_name
        user32.RegisterClassW(ctypes.byref(wc))
        style = 0x00CF0000 | 0x10000000                # WS_OVERLAPPEDWINDOW | WS_VISIBLE
        self._hwnd = user32.CreateWindowExW(0, self._class_name, title, style,
                                            60, 60, width, height, None, None, self._hinstance, None)
        if not self._hwnd:
            raise RuntimeError('CreateWindowExW failed (no interactive desktop?)')
        self._hdc = user32.GetDC(self._hwnd)
        self._memdc = gdi32.CreateCompatibleDC(self._hdc)

    def _dispatch(self, hwnd, message, wparam, lparam):
        user32 = self.user32
        gdi32 = self.gdi32
        if message == 0x000F:                            # WM_PAINT
            ps = self._PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, self.ctypes.byref(ps))
            rect = self.wintypes.RECT()
            user32.GetClientRect(hwnd, self.ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if self._memdc and self._bitmap and self._image:
                old = gdi32.SelectObject(self._memdc, self._bitmap)
                gdi32.StretchBlt(hdc, 0, 0, w, h, self._memdc, 0, 0,
                                 self._image.width, self._image.height, 0x00CC0020)  # SRCCOPY
                gdi32.SelectObject(self._memdc, old)
            user32.EndPaint(hwnd, self.ctypes.byref(ps))
            return 0
        if message == 0x0014:                            # WM_ERASEBKGND
            return 1
        if message == 0x0100:                            # WM_KEYDOWN
            if wparam in (0x1B, 0x51):                   # Esc / Q -> quit
                user32.DestroyWindow(hwnd)
                return 0
            if wparam == 0x53 and self._on_save:         # S -> save snapshot
                self._on_save()
                return 0
        if message == 0x0010:                            # WM_CLOSE
            user32.DestroyWindow(hwnd)
            return 0
        if message == 0x0002:                            # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def set_image(self, image):
        _dbg(f'set_image start {image.width}x{image.height}')
        gdi32 = self.gdi32
        if self._memdc and self._bitmap:
            gdi32.SelectObject(self._memdc, self._stock_bitmap)
            gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        import numpy as _np
        array = _np.asarray(image, dtype=_np.uint8)
        bgra = _np.ascontiguousarray(array[:, :, [2, 1, 0, 3]])
        info = self._BITMAPINFO()
        info.bmiHeader.biSize = self.ctypes.sizeof(self._BITMAPINFOHEADER)
        info.bmiHeader.biWidth = image.width
        info.bmiHeader.biHeight = -image.height           # top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0                  # BI_RGB
        bits = self.ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(self._hdc, self.ctypes.byref(info), 0,
                                      self.ctypes.byref(bits), None, 0)
        if not hbmp:
            raise RuntimeError('CreateDIBSection failed')
        self.ctypes.memmove(bits, bgra.ctypes.data, bgra.nbytes)
        self._bitmap = hbmp
        self._image = image
        self._stock_bitmap = gdi32.SelectObject(self._memdc, hbmp)
        self.user32.InvalidateRect(self._hwnd, None, True)
        _dbg('set_image done')

    def set_title(self, text):
        self.user32.SetWindowTextW(self._hwnd, text)

    def close(self):
        self.user32.DestroyWindow(self._hwnd)

    def run(self, interval=1.5, autoclose_ms=0, started=None):
        """Pump the message queue with PeekMessageW and poll on a Python cadence.

        A plain GetMessageW + SetTimer pump is avoided: WM_TIMER delivery is
        unreliable inside console-attached host processes, so the loop below
        drives everything from Python time instead.
        """
        user32 = self.user32
        self._started = started if started is not None else _time.monotonic()
        self._autoclose_ms = autoclose_ms
        last_tick = None
        if self._on_tick:
            self._on_tick()                              # initial render
        _dbg(f'run: initial tick done')
        msg = self.wintypes.MSG()
        while True:
            while user32.PeekMessageW(self.ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0012:                 # WM_QUIT
                    self.cleanup()
                    return
                user32.TranslateMessage(self.ctypes.byref(msg))
                user32.DispatchMessageW(self.ctypes.byref(msg))
            now = _time.monotonic()
            if self._on_tick and (last_tick is None or now - last_tick >= interval):
                last_tick = now
                self._on_tick()
            if autoclose_ms and now - self._started > autoclose_ms / 1000.0:
                _dbg('run: autoclose reached')
                break
            _time.sleep(0.05)
        user32.DestroyWindow(self._hwnd)
        self.cleanup()

    def cleanup(self):
        gdi32 = self.gdi32
        if self._memdc and self._bitmap:
            gdi32.SelectObject(self._memdc, self._stock_bitmap)
            gdi32.DeleteObject(self._bitmap)
        if self._memdc:
            gdi32.DeleteDC(self._memdc)
        if self._hdc:
            self.user32.ReleaseDC(self._hwnd, self._hdc)
        self._bitmap = self._memdc = self._image = None


def run_live(source, cfg, save_png, save_csv, interval=1.5, title=None, autoclose_ms=0):
    """Tail `source`; plot each new energy record in a popup window."""
    tail = LiveTail(source)
    values = []
    started = _time.monotonic()
    window = None

    def save_snapshot(why='snapshot'):
        if not values:
            return
        fig = plot_live(values, cfg, source, title)
        dpi = cfg.getint('plot', 'dpi', fallback=110)
        fig.savefig(save_png, dpi=dpi)
        plotting(cfg).close(fig)
        write_csv(save_csv, [[i, v] for i, v in enumerate(values, 1)], ['point', 'energy_eh'])
        print(f'optviewer: {why} saved: {save_png} and {save_csv}', file=sys.stderr)

    def collect():
        nonlocal values
        lines = tail.poll()
        if not lines:
            return False
        changed = False
        for line in lines:
            for pattern in _ENERGY_PATTERNS:
                match = pattern.search(line)
                if match:
                    value = number(match.group(1))
                    # Skip exact and near-identical echoes: ORCA prints each step's
                    # energy twice (Total Energy + FINAL SINGLE POINT ENERGY).
                    if math.isfinite(value) and (not values or abs(value - values[-1]) > 1e-10):
                        values.append(value)
                        changed = True
                        print(f'optviewer: live point {len(values)}: E = {value:.6f} Eh', file=sys.stderr)
                    break
        return changed

    def redraw():
        fig = plot_live(values, cfg, source, title)
        image = render_live_image(fig)
        plotting(cfg).close(fig)
        window.set_image(image)
        window.set_title(f'{source.name} | live | {len(values)} points'
                         + (f' | E = {values[-1]:.6f} Eh' if values else ' | waiting for energy records'))

    def on_tick():
        _dbg(f'tick at {(_time.monotonic() - started):.2f}s points={len(values)}')
        if collect() or window._image is None:
            redraw()

    def on_save():
        save_snapshot('snapshot')

    try:
        window = LiveWindow(title or f'optviewer live - {source.name}')
        window._on_tick = on_tick
        window._on_save = on_save
        print(f'optviewer: live tracking {source} (Esc/Q to quit, S to save snapshot)', file=sys.stderr)
        window.run(interval=interval, autoclose_ms=autoclose_ms, started=started)
    except Exception as exc:
        # Fall back to a console loop when no interactive desktop is available.
        print(f'optviewer: popup window unavailable ({exc}); using console tracking', file=sys.stderr)
        try:
            while True:
                collect()
                if autoclose_ms and _time.monotonic() - started > autoclose_ms / 1000.0:
                    break
                _time.sleep(interval)
        except KeyboardInterrupt:
            pass
    if values:
        save_snapshot('final snapshot')
    print('optviewer: live tracking finished', file=sys.stderr)
    return 0


def legacy_module():
    name = '_optviewer_legacy'
    if name not in sys.modules:
        path = Path(getattr(sys, '_MEIPASS', ROOT)) / 'legacy_optviewer.marshal'
        module = types.ModuleType(name)
        module.__file__ = str(ROOT / 'optviewer.py')
        sys.modules[name] = module
        try:
            exec(marshal.loads(path.read_bytes()), module.__dict__)
        except ValueError as exc:
            sys.modules.pop(name, None)
            raise ValueError('Original optimization renderer requires Python 3.14; use optviewer.exe.') from exc
    return sys.modules[name]


def read_config(path):
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding='utf-8')
    if not cfg.has_section('plot'):
        cfg.add_section('plot')
    os.environ.setdefault('MPLBACKEND', 'Agg')
    os.environ.setdefault('MPLCONFIGDIR', str(ROOT / '.mpl-cache'))
    return cfg


def plotting(cfg):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.style.use(cfg.get('plot', 'style', fallback='default'))
    return plt


def plot_memory(points, cfg, source, title=None):
    plt = plotting(cfg)
    fig, ax = plt.subplots(figsize=(cfg.getfloat('plot', 'width', fallback=13.2), 6.8), layout='constrained')
    x, y = list(range(1, len(points) + 1)), [point.mb for point in points]
    ax.plot(x, y, '-o', color='#2864b7', lw=1.5, ms=4)
    peak = max(range(len(points)), key=lambda i: points[i].mb)
    ax.scatter(x[peak], y[peak], color='#d23c41', s=65, zorder=4)
    ax.annotate(f'Peak: {y[peak]:,.6f}'.rstrip('0').rstrip('.') + f' MB\n{points[peak].label} | source line {points[peak].line}',
                (x[peak], y[peak]), xytext=(.53, .94), textcoords='axes fraction', va='top',
                arrowprops={'arrowstyle': '->', 'color': '#d23c41'},
                bbox={'boxstyle': 'round,pad=.5', 'fc': 'white', 'ec': '#d23c41'})
    ax.set(xlabel='Reported usage record (file order)', ylabel='Reported memory usage (MB)', ylim=(0, max(y) * 1.35 if max(y) else 1))
    ax.set_xticks(x if len(x) <= 20 else ax.get_xticks())
    ax.grid(alpha=.22)
    fig.suptitle(title or f'{source.name} | Reported memory usage', fontsize=15)
    fig.supxlabel('Log reports only; module peaks and current usage may be mixed. Values are not summed across processes.', fontsize=9)
    return fig


def plot_irc(points, thresholds, status, cfg, source, title=None):
    plt = plotting(cfg)
    fig, axes = plt.subplots(2, 2, figsize=(13.2, 8.8), layout='constrained')
    x = [p.step for p in points]
    energies = [p.energy for p in points]
    relative = [p.relative for p in points]
    for ax, y, ylabel in ((axes[0, 0], energies, 'Energy (Eh)'),
                          (axes[0, 1], relative, 'Relative energy (kcal/mol, vs TS)')):
        ax.plot(x, y, '-o', ms=3, lw=1.5, color='#2864b7')
        ax.ticklabel_format(axis='y', style='plain', useOffset=False)
        marked = {0, len(points)-1} | {i for i, p in enumerate(points) if p.direction == 'TS'}
        for i in sorted(marked):
            label = ('TS\n' if points[i].direction == 'TS' else '') + f'{y[i]:.6f}'
            ax.annotate(label, (x[i], y[i]), xytext=(0, 12), textcoords='offset points',
                        ha='center', fontsize=8, bbox={'fc': 'white', 'ec': 'none', 'alpha': .8})
        ax.set_ylabel(ylabel)
        ax.margins(x=.1, y=.22)
    for ax, attribute, threshold, label in ((axes[1, 0], 'max_gradient', thresholds[0], 'Max gradient (Eh/bohr)'),
                                            (axes[1, 1], 'rms_gradient', thresholds[1], 'RMS gradient (Eh/bohr)')):
        ax.plot(x, [getattr(p, attribute) if getattr(p, attribute) > 0 else math.nan for p in points], '-o', ms=3, lw=1.2)
        ax.set_yscale('log')
        if threshold is not None:
            ax.axhline(threshold, color='#d23c41', ls='--', label=f'Threshold {threshold:g}')
            ax.legend(fontsize=8)
        ax.set_ylabel(label)
    for ax in axes.flat:
        ax.set_xlabel('IRC path step (backward endpoint → TS → forward endpoint)')
        ax.grid(alpha=.22)
    fig.suptitle(title or f'{source.name} | IRC | {len(points)} path points', fontsize=15)
    fig.supxlabel('Backward: ' + status['Backward'] + '   |   Forward: ' + status['Forward'] + '\nAll step energies and source lines are recorded in the CSV.', fontsize=9)
    return fig


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def build_parser():
    parser = argparse.ArgumentParser(prog='optviewer', description='Plot ORCA/Gaussian optimization, ORCA IRC, reported memory usage, or watch a running calculation live.',
                                     epilog='Examples: optviewer job.out -O energy.png | optviewer job.out -M memory.png | optviewer job.out energy.png | optviewer job.out (live window)')
    parser.add_argument('input', nargs='?', type=Path, help='calculation output file (.out/.log)')
    parser.add_argument('png', nargs='?', type=Path, help='output PNG (energy mode)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-O', '-o', '--output', type=Path, help='optimization/IRC PNG destination')
    group.add_argument('-M', '-m', '--memory', type=Path, help='memory PNG destination')
    group.add_argument('-w', '--watch', action='store_true', help='live tracking window (default when no PNG is given)')
    parser.add_argument('--interval', type=float, default=1.5, metavar='SECONDS', help='live polling interval (default 1.5)')
    parser.add_argument('--csv', type=Path, help='CSV destination (default: same stem as PNG)')
    parser.add_argument('--config', type=Path, default=ROOT / 'optviewer.ini')
    parser.add_argument('--dpi', type=int)
    parser.add_argument('--title')
    parser.add_argument('--summary', action='store_true')
    parser.add_argument('--init', action='store_true')
    parser.add_argument('--version', action='version', version=f'%(prog)s {VERSION}')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.init:
        return legacy_module().main(['--config', str(args.config), '--init'])
    if args.input is None:
        parser.error('input file is required')
    if args.png and (args.output or args.memory):
        parser.error('specify the PNG once: INPUT -O PNG, INPUT -M PNG, or INPUT PNG')
    destination = args.memory or args.output or args.png
    if args.watch or destination is None:
        if args.interval <= 0:
            parser.error('--interval must be positive')
        try:
            cfg = read_config(args.config)
            try:
                autoclose = int(os.environ.get('OPTVIEWER_AUTOCLOSE_MS', '0') or 0)
            except ValueError:
                autoclose = 0
            save_png = destination or args.input.with_name(args.input.stem + '_live.png')
            save_csv = args.csv or args.input.with_name(args.input.stem + '_live.csv')
            return run_live(args.input, cfg, save_png, save_csv, interval=args.interval,
                            title=args.title, autoclose_ms=autoclose)
        except Exception as exc:
            import traceback as _tb
            _tb.print_exc()
            print(f'optviewer: error: {exc}', file=sys.stderr)
            return 1
    if destination.suffix.lower() != '.png':
        parser.error('the output image must have a .png extension')
    csv_path = args.csv or destination.with_suffix('.csv')
    if args.dpi is not None and args.dpi <= 0:
        parser.error('--dpi must be positive')
    paths = [args.input.resolve(), destination.resolve(), csv_path.resolve(), args.config.resolve()]
    if len(set(paths)) != len(paths):
        parser.error('input, PNG, CSV, and configuration must be different files')
    try:
        # Preserve literal LF line numbers: ORCA also emits standalone CR progress updates.
        text = args.input.read_bytes().decode('utf-8-sig', errors='replace')
        cfg = read_config(args.config)
        if args.memory:
            points = parse_memory(text)
            if not points:
                raise ValueError('No reported memory usage with units found; configured limits such as MaxCore are excluded.')
            fig = plot_memory(points, cfg, args.input, args.title)
            fields = ['record', 'source_line', 'memory_mb', 'module_or_label', 'original_log_line']
            rows = [[i, p.line, p.mb, p.label, p.text] for i, p in enumerate(points, 1)]
            peak = max(points, key=lambda p: p.mb)
            summary = f'{len(points)} memory records; peak={peak.mb:g} MB (line {peak.line}, {peak.label})'
        else:
            points, thresholds, status = parse_irc(text)
            if points:
                fig = plot_irc(points, thresholds, status, cfg, args.input, args.title)
                fields = ['path_step', 'direction', 'direction_iteration', 'energy_eh', 'relative_energy_kcal_mol', 'max_gradient_eh_bohr', 'rms_gradient_eh_bohr', 'source_line']
                rows = [[p.step, p.direction, p.iteration, p.energy, p.relative, p.max_gradient, p.rms_gradient, p.line] for p in points]
                summary = f'IRC: {len(points)} points; backward={status["Backward"]}; forward={status["Forward"]}'
            elif re.search(r'Intrinsic Reaction Coordinate Calculation|\b(?:FORWARD|BACKWARD) IRC\b|IRC PATH SUMMARY', text, re.I):
                raise ValueError('IRC calculation detected, but no IRC step energies have been printed yet.')
            else:
                legacy = legacy_module()
                result = legacy.parse_output(text, str(args.input))
                fig = legacy.plot_result(result, cfg, args.title)
                fields = ['step', 'energy_eh'] + list(legacy.METRICS)
                rows = [[i, p.energy] + [p.values.get(key, '') for key in legacy.METRICS] for i, p in enumerate(result.steps, 1)]
                summary = f'{result.program}: {len(result.steps)} optimization steps; converged={result.converged}'
        dpi = args.dpi or cfg.getint('plot', 'dpi', fallback=110)
        if dpi <= 0:
            raise ValueError('DPI must be positive')
        destination.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(destination, dpi=dpi, transparent=cfg.getboolean('plot', 'transparent', fallback=False), metadata={'Software': f'optviewer {VERSION}'})
        plotting(cfg).close(fig)
        write_csv(csv_path, rows, fields)
        print(f'optviewer: wrote {destination} and {csv_path}', file=sys.stderr)
        if args.summary:
            print(summary, file=sys.stderr)
        return 0
    except Exception as exc:
        print(f'optviewer: error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
