"""
Client module for interacting with streaming tasks.
Provides a shared httpx client for efficient connection pooling.
"""
import asyncio
import json
from typing import Optional, Any, Dict, AsyncIterator
import httpx


class StreamingClient:
    """
    Singleton client for interacting with streaming tasks.
    Maintains a single httpx client per process for efficient connection pooling.
    """
    _instance = None
    _client: Optional[httpx.AsyncClient] = None
    _sync_client: Optional[httpx.Client] = None

    def __new__(cls, base_url: str = "http://127.0.0.1:8888"):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, base_url: str = "http://127.0.0.1:8888"):
        """
        Initialize the streaming client.

        Args:
            base_url: Base URL for the streaming server
        """
        if not hasattr(self, '_initialized'):
            self.base_url = base_url.rstrip('/')
            self._initialized = True

    @property
    def async_client(self) -> httpx.AsyncClient:
        """Get or create the shared async httpx client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=5.0),
                follow_redirects=True
            )
        return self._client

    @property
    def sync_client(self) -> httpx.Client:
        """Get or create the shared sync httpx client."""
        if self._sync_client is None:
            self._sync_client = httpx.Client(
                timeout=httpx.Timeout(60.0, connect=5.0),
                follow_redirects=True
            )
        return self._sync_client

    async def aclose(self):
        """Close the async client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def close(self):
        """Close the sync client."""
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    async def get_task_status(
        self,
        app_name: str,
        model_name: str,
        task_id: int
    ) -> Dict[str, Any]:
        """
        Get the status of a completed task.

        Args:
            app_name: The Django app name
            model_name: The model name
            task_id: The task ID

        Returns:
            Dict containing task status (only works for completed tasks)

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/stream/{app_name}/{model_name}/{task_id}"
        response = await self.async_client.get(url)
        response.raise_for_status()
        return response.json()

    def get_task_status_sync(
        self,
        app_name: str,
        model_name: str,
        task_id: int
    ) -> Dict[str, Any]:
        """
        Get the status of a completed task (synchronous).

        Args:
            app_name: The Django app name
            model_name: The model name
            task_id: The task ID

        Returns:
            Dict containing task status (only works for completed tasks)

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/stream/{app_name}/{model_name}/{task_id}"
        response = self.sync_client.get(url)
        response.raise_for_status()
        return response.json()


# Global singleton instance
_client_instance: Optional[StreamingClient] = None


def get_client(base_url: str = "http://127.0.0.1:8888") -> StreamingClient:
    """
    Get the shared streaming client instance (singleton).

    This client is shared across your entire process for efficient connection pooling.

    Args:
        base_url: Base URL for the streaming server

    Returns:
        StreamingClient singleton instance

    Example:
        ```python
        from streaming import get_client

        client = get_client()
        # Use client.async_client for httpx requests
        response = await client.async_client.get("https://api.example.com/data")
        ```
    """
    global _client_instance
    if _client_instance is None:
        _client_instance = StreamingClient(base_url)
    return _client_instance
