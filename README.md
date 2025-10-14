# Django Streaming Coordinator

A Django-based system for managing long-running tasks with Server-Sent Events (SSE) streaming. Tasks continue running even if clients disconnect, and multiple clients can connect to the same task simultaneously.

## Features

- **Persistent Tasks**: Tasks continue running in the background even when clients disconnect
- **Multiple Clients**: Multiple clients can connect to the same task and receive real-time updates
- **Server-Sent Events (SSE)**: Uses SSE for efficient real-time streaming
- **Easy Task Creation**: Simple API for creating custom streaming tasks
- **Unix Socket Support**: Can bind to Unix sockets or TCP ports
- **Async/Await**: Built on modern Python asyncio for efficient concurrent operations

## Installation

```bash
# Install dependencies
poetry install

# Run migrations
poetry run python manage.py migrate
```

## Quick Start

### 1. Create a Custom Task

Subclass `StreamTask` and implement the `async process()` method:

```python
from streaming.models import StreamTask
import asyncio

class MyCustomTask(StreamTask):
    # Add your custom fields
    title = models.CharField(max_length=255)

    async def process(self):
        # Send start event
        await self.send_event('start', {
            'message': f'Starting task: {self.title}'
        })

        # Do your work and send events
        for i in range(10):
            await asyncio.sleep(1)
            await self.send_event('progress', {
                'step': i + 1,
                'total': 10,
                'message': f'Processing step {i + 1}'
            })

        # Send completion event
        await self.send_event('complete', {
            'message': 'Task completed successfully'
        })
```

### 2. Run the Streaming Server

```bash
# Using Unix socket (default)
poetry run python manage.py runserver_stream

# Using TCP port
poetry run python manage.py runserver_stream --host 127.0.0.1 --port 8888

# Custom Unix socket path
poetry run python manage.py runserver_stream --socket /tmp/my-stream.sock
```

### 3. Create and Connect to a Task

```python
# Create a task instance
from streaming.models import ExampleTask
from streaming.coordinator import coordinator

task = ExampleTask.objects.create(message="Hello, World!")

# Start the task (will run in background)
await coordinator.start_task(task, 'ExampleTask')

# Connect via HTTP SSE
# GET /stream/ExampleTask/{task_id}
```

### 4. Connect from Client

Using JavaScript:

```javascript
const eventSource = new EventSource('/stream/ExampleTask/1');

eventSource.addEventListener('start', (event) => {
    const data = JSON.parse(event.data);
    console.log('Task started:', data);
});

eventSource.addEventListener('progress', (event) => {
    const data = JSON.parse(event.data);
    console.log('Progress:', data);
});

eventSource.addEventListener('complete', (event) => {
    const data = JSON.parse(event.data);
    console.log('Task completed:', data);
    eventSource.close();
});

eventSource.addEventListener('error', (event) => {
    const data = JSON.parse(event.data);
    console.error('Error:', data);
    eventSource.close();
});
```

Using Python (httpx + httpx-sse):

```python
import httpx
from httpx_sse import aconnect_sse
import json

async with httpx.AsyncClient() as client:
    async with aconnect_sse(
        client, 'GET', 'http://127.0.0.1:8888/stream/ExampleTask/1'
    ) as event_source:
        async for event in event_source.aiter_sse():
            data = json.loads(event.data)
            print(f"{event.event}: {data}")

            if event.event == 'complete':
                break
```

## API

### StreamTask Model

Base abstract model for all streaming tasks.

**Fields:**
- `created_at`: Timestamp when task was created
- `updated_at`: Timestamp when task was last updated
- `completed_at`: Timestamp when task completed (null if not completed)

**Methods:**

#### `async send_event(event_type: str, data: dict)`
Send an event to all connected clients.

**Parameters:**
- `event_type`: Type of event (e.g., 'start', 'progress', 'complete', 'error')
- `data`: Dictionary of data to send to clients

**Example:**
```python
await self.send_event('progress', {
    'step': 5,
    'total': 10,
    'message': 'Halfway there!'
})
```

#### `async process()`
Override this method to implement your task logic. This is where your task's work happens.

#### `async mark_completed()`
Mark the task as completed. Called automatically by the coordinator when `process()` finishes.

### TaskCoordinator

Singleton that manages running tasks.

**Methods:**

#### `async start_task(task_instance: StreamTask, model_name: str)`
Start a task in the background.

#### `async get_task_instance(model_name: str, task_id: int) -> StreamTask`
Get a running task instance or load from database.

#### `is_task_running(model_name: str, task_id: int) -> bool`
Check if a task is currently running.

## HTTP Endpoints

### `GET /stream/{model_name}/{task_id}`
Connect to a task's SSE stream.

**Response Format:**
```
event: start
data: {"message": "...", "_task_id": 1, "_model": "ExampleTask", "_timestamp": "..."}

event: progress
data: {"step": 1, "total": 3, "_task_id": 1, "_model": "ExampleTask", "_timestamp": "..."}

event: complete
data: {"message": "...", "_task_id": 1, "_model": "ExampleTask", "_timestamp": "..."}
```

### `GET /health`
Health check endpoint. Returns `200 OK`.

## Testing

Run tests with Django's test runner:

```bash
# Run all tests
poetry run python manage.py test

# Run specific test class
poetry run python manage.py test streaming.tests.StreamingSystemTests

# Run with verbose output
poetry run python manage.py test --verbosity=2
```

## Architecture

1. **StreamTask**: Abstract Django model that defines the interface for streaming tasks
2. **TaskCoordinator**: Singleton that manages task lifecycle and keeps tasks running
3. **SSE Server**: asgineer-based ASGI server that handles HTTP SSE connections
4. **Management Command**: `runserver_stream` to start the server on Unix socket or TCP port

## Example: ExampleTask

The project includes a simple example task:

```python
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
                'message': f"Step {i} of 3"
            })

        await self.send_event('complete', {
            'message': 'Task completed successfully'
        })
```

Test it:

```bash
# Start server
poetry run python manage.py runserver_stream --port 8888

# In another terminal, create a task
poetry run python manage.py shell
>>> from streaming.models import ExampleTask
>>> from streaming.coordinator import coordinator
>>> import asyncio
>>> task = ExampleTask.objects.create(message="Test")
>>> asyncio.run(coordinator.start_task(task, 'ExampleTask'))

# Connect with curl or browser
curl http://127.0.0.1:8888/stream/ExampleTask/1
```

## Requirements

- Python 3.11+
- Django 5.2+
- asgineer
- uvicorn
- httpx (for testing)
- httpx-sse (for testing)

## License

MIT
