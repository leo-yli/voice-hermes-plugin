try:
    from .adapter import register
except ImportError:  # Allows pytest to import this root __init__.py directly.
    from adapter import register

__all__ = ["register"]
