import asyncio
import json
import logging
import traceback
from abc import ABCMeta, abstractmethod
from typing import AsyncGenerator, Generator, Any
from django.db import models
from django.utils import timezone

logger = logging.getLogger('streaming.tasks')


class StreamTaskMeta(ABCMeta, type(models.Model)):
    
    pass


class StreamTask(models.Model, metaclass=StreamTaskMeta):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    final_value = models.JSONField(null=True, blank=True)

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._clients = set()  
        self._latest_data = None  

    async def send_event(self, event_type: str, data: dict):
        event_data = {
            'type': event_type,
            'data': data,
            'timestamp': timezone.now().isoformat()
        }

        # Log at DEBUG level to avoid verbosity (important events logged elsewhere)
        logger.debug(f"Task {self.pk} sending '{event_type}' event to {len(self._clients)} client(s)")

        # Cache latest data for new clients
        self._latest_data = event_data


        if not self._clients:
            logger.debug(f"Task {self.pk} has no connected clients, event cached only")
            return


        async def send_to_client(queue):

            try:
                await queue.put(event_data)
                return (queue, None)
            except Exception as e:
                # Print traceback for visibility
                print(f"\nTask {self.pk} failed to send event to client:", flush=True)
                traceback.print_exc()

                logger.error(f"Task {self.pk} failed to send event to client: {type(e).__name__}: {str(e)}")
                return (queue, e)



        results = await asyncio.gather(*[send_to_client(q) for q in self._clients])


        failed_clients = 0
        for queue, error in results:
            if error is not None:
                failed_clients += 1
                self._clients.discard(queue)

        if failed_clients > 0:
            logger.warning(f"Task {self.pk} removed {failed_clients} failed client(s), {len(self._clients)} remaining")

    async def add_client(self, queue):
        logger.info(f"Task {self.pk} adding client (total: {len(self._clients) + 1})")
        self._clients.add(queue)
        latest = self._latest_data


        if latest:
            try:
                logger.debug(f"Task {self.pk} sending latest cached event to new client")
                await queue.put(latest)
            except Exception as e:
                # Print traceback for visibility
                print(f"\nTask {self.pk} failed to send cached event to new client:", flush=True)
                traceback.print_exc()

                logger.error(f"Task {self.pk} failed to send cached event to new client: {type(e).__name__}: {str(e)}")
                self._clients.discard(queue)

    async def remove_client(self, queue):
        logger.info(f"Task {self.pk} removing client (total: {len(self._clients) - 1})")
        self._clients.discard(queue)

    @abstractmethod
    async def process(self):
        pass

    async def mark_completed(self, final_value=None):
        logger.info(f"Task {self.pk} marking as completed with final value: {final_value}")
        self.completed_at = timezone.now()
        self.final_value = final_value
        await self.asave(update_fields=['completed_at', 'final_value', 'updated_at'])

    async def process_generator(self, generator: AsyncGenerator[dict, None]) -> Any:
        """
        Process an async generator, sending each yielded value as an event.

        Args:
            generator: An async generator that yields dict values

        Returns:
            The final value (last yielded value or None)
        """
        final_value = None
        async for value in generator:
            await self.send_event('progress', value)
            final_value = value
        return final_value

    async def process_sync_generator(self, generator: Generator[dict, None, None]) -> Any:
        """
        Process a sync generator, sending each yielded value as an event.

        Args:
            generator: A sync generator that yields dict values

        Returns:
            The final value (last yielded value or None)
        """
        final_value = None
        for value in generator:
            await self.send_event('progress', value)
            final_value = value
        return final_value
