import asyncio
import json
from abc import ABCMeta, abstractmethod
from django.db import models
from django.utils import timezone


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

        
        self._latest_data = event_data

        
        if not self._clients:
            return

        
        async def send_to_client(queue):
            
            try:
                await queue.put(event_data)
                return (queue, None)
            except Exception as e:
                return (queue, e)

        
        
        results = await asyncio.gather(*[send_to_client(q) for q in self._clients])

        
        for queue, error in results:
            if error is not None:
                self._clients.discard(queue)

    async def add_client(self, queue):
        
        self._clients.add(queue)
        latest = self._latest_data  

        
        if latest:
            try:
                await queue.put(latest)
            except Exception:
                
                self._clients.discard(queue)

    async def remove_client(self, queue):
        
        self._clients.discard(queue)

    @abstractmethod
    async def process(self):
        pass

    async def mark_completed(self, final_value=None):

        self.completed_at = timezone.now()
        self.final_value = final_value
        await self.asave(update_fields=['completed_at', 'final_value', 'updated_at'])


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


        final_result = {
            'message': 'Task completed successfully',
            'total_steps': 3,
            'completed': True
        }
        await self.send_event('complete', final_result)

        return final_result


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
            final_result = {
                'message': 'Timeout waiting for continue signal',
                'timed_out': True
            }
            await self.send_event('timeout', final_result)
            return final_result
        else:

            final_result = {
                'message': 'Task completed successfully',
                'continue_received': True
            }
            await self.send_event('complete', final_result)
            return final_result
