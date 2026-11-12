"""Local persistence prototype; see storage/README.md."""
from .repository import Store, StorageError

__all__ = ["Store", "StorageError"]
