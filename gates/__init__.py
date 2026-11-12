"""Server-side gate records. Telegram transport must authenticate updates."""
from .store import GateError, GateStore

__all__ = ["GateError", "GateStore"]
