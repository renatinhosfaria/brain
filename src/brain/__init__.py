"""Brain: capability-scoped longitudinal conversation memory for Hermes."""

__version__ = "0.1.0"

from .config import BrainSettings
from .service import BrainService

__all__ = ["BrainService", "BrainSettings", "__version__"]
