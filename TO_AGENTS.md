# Django Streaming Coordinator - Quick Reference for Agents

## What is this?

A Django library for managing long-running tasks with real-time Server-Sent Events (SSE) streaming. Tasks run persistently in the background, even when clients disconnect, and multiple clients can connect to the same task stream.

## Creating a Task

### 1. Define a Task Model

Subclass `StreamTask` and implement the `async process()` method:

```python
from streaming.models import StreamTask
from django.db import models
import asyncio

class MyTask(StreamTask):
    # Add custom fields
    message = models.CharField(max_length=255)

    async def process(self):
        # Send events to connected clients
        await self.send_event('start', {'message': self.message})

        # Do work
        for i in range(5):
            await asyncio.sleep(1)
            await self.send_event('progress', {
                'step': i + 1,
                'total': 5
            })

        await self.send_event('complete', {'result': 'done'})
        return {'final': 'value'}  # Stored in task.final_value
```

### 2. Helper Methods

**Process async generators** (auto-sends progress events):
```python
async def process(self):
    async def my_generator():
        for i in range(10):
            await asyncio.sleep(0.1)
            yield {'step': i, 'data': f'Step {i}'}

    return await self.process_generator(my_generator())
```

**Process sync generators**:
```python
async def process(self):
    def my_generator():
        for item in items:
            yield {'item': item}

    return await self.process_sync_generator(my_generator())
```

## Starting a Task

### Create and Start
```python
from tests.models import MyTask
from streaming.coordinator import coordinator

# Create task via Django ORM
task = await MyTask.objects.acreate(message="Hello!")

# Start task (runs in background)
await coordinator.start_task(task, 'app_name', 'MyTask')
```

## Streaming Events

### Server Setup
```bash
# Start the streaming server
poetry run python manage.py runserver_stream --port 8888
```

### Client Connection

**JavaScript:**
```javascript
const eventSource = new EventSource('/stream/app_name/MyTask/1');
eventSource.addEventListener('progress', (e) => {
    const data = JSON.parse(e.data);
    console.log(data);
});
```

**Python:**
```python
import httpx
from httpx_sse import aconnect_sse

async with httpx.AsyncClient() as client:
    async with aconnect_sse(client, 'GET',
        'http://127.0.0.1:8888/stream/app_name/MyTask/1') as events:
        async for event in events.aiter_sse():
            print(f"{event.event}: {event.data}")
```

## Key API

### StreamTask Methods
- `await self.send_event(event_type, data)` - Send event to all connected clients
- `await self.process_generator(async_gen)` - Process async generator with auto-events
- `await self.process_sync_generator(sync_gen)` - Process sync generator with auto-events
- `await self.mark_completed(final_value)` - Mark task complete (auto-called)

### Coordinator
```python
from streaming.coordinator import coordinator

# Start task
await coordinator.start_task(task, 'app_name', 'ModelName')

# Check if running
coordinator.is_task_running('app_name', 'ModelName', task_id)

# Get task instance
task = await coordinator.get_task_instance('app_name', 'ModelName', task_id)
```

### HTTP Client (for tasks making HTTP requests)
```python
from streaming import get_client

client = get_client()

# Use shared httpx client
response = await client.async_client.get("https://api.example.com/data")
data = response.json()
```

## Stream Endpoint
```
GET /stream/{app_name}/{model_name}/{task_id}
```

Returns SSE stream if task is running, or JSON status if completed.

## Event Format
All events include metadata:
```json
{
  "type": "progress",
  "data": {"step": 1, "message": "Processing..."},
  "timestamp": "2025-01-15T12:34:56.789Z",
  "_task_id": 1,
  "_app": "app_name",
  "_model": "ModelName"
}
```
