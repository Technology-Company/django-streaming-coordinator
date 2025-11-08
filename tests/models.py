import asyncio
import httpx
from django.db import models
from streaming.models import StreamTask


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
    """Example task using httpx to fetch data from an API."""
    url = models.URLField(default="https://httpbin.org/json")

    async def process(self):
        await self.log('info', f'Starting HTTP fetch from {self.url}', url=self.url)
        await self.send_event('start', {'url': self.url})

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Send progress event
                await self.log('info', 'Fetching data...', status='fetching')
                await self.send_event('progress', {
                    'status': 'fetching',
                    'message': f'Fetching data from {self.url}'
                })

                # Fetch the data
                response = await client.get(self.url)
                response.raise_for_status()

                data = response.json()

                # Send progress with data info
                await self.log('info', f'Data fetched successfully (status: {response.status_code}, size: {len(str(data))} bytes)')
                await self.send_event('progress', {
                    'status': 'fetched',
                    'status_code': response.status_code,
                    'data_size': len(str(data)),
                })

                # Complete
                await self.log('info', 'HTTP fetch completed successfully')
                await self.send_event('complete', {
                    'message': 'Data fetched successfully',
                    'url': self.url
                })

                return data

        except httpx.HTTPError as e:
            await self.log('error', f'HTTP error occurred: {str(e)}', url=self.url, error_type=type(e).__name__)
            await self.send_event('error', {
                'message': f'HTTP error: {str(e)}',
                'url': self.url
            })
            raise
