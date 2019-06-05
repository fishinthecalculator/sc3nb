"""Collection of Classes and functions for communicating
with SuperCollider within python and jupyter notebooks,
as well as playing recording and visualizing audio
"""

from .helpers import *

from .sc import startup, SC
from .timed_queue import TimedQueue, TimedQueueSC
from .buffer import Buffer
from .osc_communication import build_message, build_bundle