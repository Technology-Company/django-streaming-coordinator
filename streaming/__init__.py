__version__ = "0.1.0"

__all__ = [
    "StreamingClient",
    "get_client",
    "StreamTask",
    "__version__",
]


def __getattr__(name):
    """Lazy import to avoid circular import issues with Django."""
    if name == "StreamTask":
        from streaming.models import StreamTask
        return StreamTask
    elif name in ["StreamingClient", "get_client"]:
        from streaming import client
        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
