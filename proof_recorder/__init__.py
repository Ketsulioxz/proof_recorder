
from . import _compat        # noqa: F401
from . import config         # noqa: F401
from . import proof_xlsx     # noqa: F401
from . import proof_sheet    # noqa: F401
from . import mobile         # noqa: F401
from . import capture        # noqa: F401
from . import pageutil       # noqa: F401
from . import winmgr         # noqa: F401
from . import network        # noqa: F401
from . import classify       # noqa: F401
from . import dommap         # noqa: F401
from . import browser        # noqa: F401
from . import transition     # noqa: F401
from . import pipeline       # noqa: F401
from . import batch          # noqa: F401
from . import gui            # noqa: F401

_submodules = (config, proof_xlsx, proof_sheet, mobile, capture, pageutil,
               winmgr, network, classify, dommap, browser, transition,
               pipeline, batch, gui)
for _m in _submodules:
    for _k, _v in vars(_m).items():
        if not _k.startswith("__"):
            globals()[_k] = _v
del _m, _k, _v, _submodules
