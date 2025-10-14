# Quick Start Guide

Get up and running with the Django Streaming Coordinator in 2 minutes!

## Installation

```bash
poetry install
poetry run python manage.py migrate
```

## Basic Usage

### Step 1: Start the Server

```bash
poetry run python manage.py runserver_stream --port 8888
```

Keep this terminal open.

### Step 2: Create a Task (in a new terminal)

```bash
poetry run python manage.py create_task --type example --message "My first task"
```

Output:
```
Created ExampleTask with ID: 1
✓ Task started
Connect to task stream:
  curl http://127.0.0.1:8888/stream/ExampleTask/1
```

### Step 3: Connect to the Stream (in another terminal)

**Option A: Using the Python client (recommended)**

```bash
poetry run python stream_client.py ExampleTask 1
```

**Option B: Using curl**

```bash
curl http://127.0.0.1:8888/stream/ExampleTask/1
```

You'll see events streaming in real-time!

## Creating Your Own Task

1. **Create a new model** in `streaming/models.py`:

```python
class MyTask(StreamTask):
    # Your custom fields
    title = models.CharField(max_length=255)

    async def process(self):
        # Send start event
        await self.send_event('start', {'title': self.title})

        # Do your work
        for i in range(5):
            await asyncio.sleep(1)
            await self.send_event('progress', {'step': i + 1})

        # Send completion
        await self.send_event('complete', {'message': 'Done!'})
```

2. **Create migrations**:

```bash
poetry run python manage.py makemigrations
poetry run python manage.py migrate
```

3. **Use it**:

```python
from streaming.models import MyTask
from streaming.coordinator import coordinator
import asyncio

# Create
task = await MyTask.objects.acreate(title="My Task")

# Start
await coordinator.start_task(task, 'MyTask')

# Connect: http://127.0.0.1:8888/stream/MyTask/1
```

## Key Concepts

- **Tasks continue running** even if clients disconnect
- **Multiple clients** can connect to the same task
- **New clients** receive the latest event immediately
- **Events** are sent via Server-Sent Events (SSE)
- **Database-driven control** - update fields to control running tasks

## Next Steps

- See [README.md](README.md) for detailed documentation
- See [TESTING.md](TESTING.md) for comprehensive testing guide
- Check out `ExampleTask` and `ContinueTask` for examples

## Useful Commands

```bash
# Create ExampleTask
poetry run python manage.py create_task --type example

# Create ContinueTask
poetry run python manage.py create_task --type continue

# Connect with Python client
poetry run python stream_client.py <ModelName> <task_id>

# Connect with curl
curl http://127.0.0.1:8888/stream/<ModelName>/<task_id>

# Health check
curl http://127.0.0.1:8888/health

# Run tests
poetry run python manage.py test
```

That's it! You're ready to build real-time streaming tasks with Django!
