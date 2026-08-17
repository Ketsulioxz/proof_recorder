
try:
    import imageio_ffmpeg
    FFMPEG_OK = True
except ImportError:
    imageio_ffmpeg = None
    FFMPEG_OK = False

try:
    import mss
    import mss.tools
    MSS_OK = True
except ImportError:
    mss = None
    MSS_OK = False

try:
    import oxipng            # сжатие PNG БЕЗ потерь (движок oxipng, Rust)
    OXIPNG_OK = True
except ImportError:
    oxipng = None
    OXIPNG_OK = False

try:
    import win32gui
    import win32con
    import win32process
    import win32api
    WIN32_OK = True
except ImportError:
    win32gui = win32con = win32process = win32api = None
    WIN32_OK = False

try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    psutil = None
    PSUTIL_OK = False
