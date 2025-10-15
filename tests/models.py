import asyncio
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

            await asyncio.sleep(2)


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


            await asyncio.sleep(0.5)
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
