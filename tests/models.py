import asyncio
import httpx
import logging
from django.db import models
from streaming.models import StreamTask

logger = logging.getLogger('streaming.tasks')


class ExampleTask(StreamTask):
    message = models.CharField(max_length=255, default="Hello from ExampleTask")

    async def process(self):
        await self.send_event('start', {
            'message': self.message,
            'total_steps': 3
        })

        for i in range(1, 4):

            await asyncio.sleep(0.01)


            await self.send_event('progress', {
                'step': i,
                'total_steps': 3,
                'message': f"Step {i} of 3",
                'data': f"Processing step {i}..."
            })


        await self.send_event('complete', {
            'message': 'Task completed successfully',
            'total_steps': 3
        })

        return f"Completed processing: {self.message}"


class ContinueTask(StreamTask):
    continue_field = models.BooleanField(default=False, db_column='continue')
    message = models.CharField(max_length=255, default="Waiting for continue signal")

    async def process(self):

        await self.send_event('started', {
            'message': self.message,
            'continue': self.continue_field
        })


        max_iterations = 100
        iteration = 0

        while iteration < max_iterations:

            await self.arefresh_from_db()

            if self.continue_field:

                await self.send_event('final', {
                    'message': 'Continue signal received',
                    'continue': True
                })
                break


            await asyncio.sleep(0.05)
            iteration += 1

        if iteration >= max_iterations:
            await self.send_event('timeout', {
                'message': 'Timeout waiting for continue signal'
            })
            return "Timeout - continue signal not received"
        else:

            await self.send_event('complete', {
                'message': 'Task completed successfully'
            })
            return "Continue signal received - task completed"


class AsyncGeneratorTask(StreamTask):
    """Example task using async generator to yield progress updates."""
    count = models.IntegerField(default=5)

    async def process(self):
        async def progress_generator():
            """Async generator that yields progress updates."""
            for i in range(self.count):
                await asyncio.sleep(0.1)
                yield {
                    'step': i + 1,
                    'total': self.count,
                    'message': f'Processing step {i + 1} of {self.count}',
                    'percentage': ((i + 1) / self.count) * 100
                }

        # Use the built-in async generator processor
        await self.send_event('start', {'total_steps': self.count})
        final_value = await self.process_generator(progress_generator())
        await self.send_event('complete', {'message': 'All steps completed'})
        return final_value


class SyncGeneratorTask(StreamTask):
    """Example task using sync generator to yield progress updates."""
    items = models.JSONField(default=list)

    async def process(self):
        def item_processor():
            """Sync generator that yields results for each item."""
            for idx, item in enumerate(self.items):
                # Simulate processing
                result = {
                    'index': idx,
                    'item': item,
                    'processed': f'Processed: {item}',
                }
                yield result

        # Use the built-in sync generator processor
        await self.send_event('start', {'total_items': len(self.items)})
        final_value = await self.process_sync_generator(item_processor())
        await self.send_event('complete', {'message': f'Processed {len(self.items)} items'})
        return final_value


class HttpxFetchTask(StreamTask):
    """Example task using httpx to fetch data from an API.

    This demonstrates proper separation of concerns:
    - Server-side logging uses logger.info/error for ops/debugging
    - Client events use send_event() for user-facing progress updates
    """
    url = models.URLField(default="https://httpbin.org/json")

    async def process(self):
        # Server-side logging (for developers/ops)
        logger.info(f"Task {self.pk}: Starting HTTP fetch from {self.url}")

        # Client event (for end users)
        await self.send_event('start', {'url': self.url})

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Server-side log
                logger.info(f"Task {self.pk}: Fetching data from {self.url}")

                # Client event
                await self.send_event('progress', {
                    'status': 'fetching',
                    'message': f'Fetching data from {self.url}'
                })

                # Fetch the data
                response = await client.get(self.url)
                response.raise_for_status()

                data = response.json()

                # Server-side log with details
                logger.info(
                    f"Task {self.pk}: Data fetched successfully "
                    f"(status: {response.status_code}, size: {len(str(data))} bytes)"
                )

                # Client event
                await self.send_event('progress', {
                    'status': 'fetched',
                    'status_code': response.status_code,
                    'data_size': len(str(data)),
                })

                # Server-side log
                logger.info(f"Task {self.pk}: HTTP fetch completed successfully")

                # Client event
                await self.send_event('complete', {
                    'message': 'Data fetched successfully',
                    'url': self.url
                })

                return data

        except httpx.HTTPError as e:
            # Server-side error log with full context
            logger.error(
                f"Task {self.pk}: HTTP error occurred: {type(e).__name__}: {str(e)}",
                exc_info=True,
                extra={'url': self.url, 'error_type': type(e).__name__}
            )

            # Client error event
            await self.send_event('error', {
                'message': f'HTTP error: {str(e)}',
                'url': self.url
            })
            raise
