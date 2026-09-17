"""FlightPath: camera-based golf launch monitor prototype.

V0 runs offline on clips recorded with a phone's stock high-speed camera.
Architecture borrowed from OpenFlight (impact trigger -> buffered frames ->
extract launch conditions -> ballistic carry -> session summary), with the
radar front end swapped for computer vision.
"""

from . import calibrate, detect, physics, session  # noqa: F401

__version__ = "0.1.9"
