# Testing Guide

This guide shows you how to test the Django Streaming Coordinator system manually.

## Prerequisites

Make sure you've installed dependencies and run migrations:

```bash
poetry install
poetry run python manage.py migrate
```

## Quick Test (All-in-One)

### 1. Start the Server

Open a terminal and start the streaming server:

```bash
poetry run python manage.py runserver_stream --port 8888
```

You should see:
```
Starting streaming server on 127.0.0.1:8888
```

Keep this terminal running.

### 2. Create and Start a Task

Open a **second terminal** and create an example task:

```bash
poetry run python manage.py create_task --type example --message "Hello, World!"
```

Output:
```
Created ExampleTask with ID: 1
  Message: Hello, World!

Starting task...
✓ Task started

Connect to task stream:
  curl http://127.0.0.1:8888/stream/ExampleTask/1
```

### 3. Connect to the Stream

In a **third terminal**, connect to the task using the provided client:

```bash
poetry run python stream_client.py ExampleTask 1
```

You should see events streaming in real-time:
```
============================================================
Connecting to: http://127.0.0.1:8888/stream/ExampleTask/1
============================================================

[1] Event: START
    Timestamp: 2025-10-14T19:24:26.897444+00:00
    message: Hello, World!
    total_steps: 3

[2] Event: PROGRESS
    Timestamp: 2025-10-14T19:24:28.898953+00:00
    step: 1
    total_steps: 3
    message: Step 1 of 3
    data: Processing step 1...

[3] Event: PROGRESS
    Timestamp: 2025-10-14T19:24:30.899625+00:00
    step: 2
    total_steps: 3
    message: Step 2 of 3
    data: Processing step 2...

[4] Event: PROGRESS
    Timestamp: 2025-10-14T19:24:32.900422+00:00
    step: 3
    total_steps: 3
    message: Step 3 of 3
    data: Processing step 3...

[5] Event: COMPLETE
    Timestamp: 2025-10-14T19:24:32.900495+00:00
    message: Task completed successfully
    total_steps: 3

============================================================
Stream ended with: COMPLETE
Total events received: 5
============================================================
```

## Test Multiple Clients

### Test 1: Connect Multiple Clients to Same Task

1. Start the server (if not already running)
2. Create a task:
   ```bash
   poetry run python manage.py create_task --type example
   ```

3. Open **multiple terminals** and connect them all to the same task:
   ```bash
   # Terminal 1
   poetry run python stream_client.py ExampleTask 2

   # Terminal 2
   poetry run python stream_client.py ExampleTask 2

   # Terminal 3
   poetry run python stream_client.py ExampleTask 2
   ```

All clients should receive the same events!

### Test 2: Late-Joining Client

1. Create a task:
   ```bash
   poetry run python manage.py create_task --type example
   ```

2. Connect first client immediately:
   ```bash
   poetry run python stream_client.py ExampleTask 3
   ```

3. Wait 3-4 seconds, then connect a second client:
   ```bash
   poetry run python stream_client.py ExampleTask 3
   ```

The second client should immediately receive the latest event, then continue receiving new events!

## Test ContinueTask (Database-Driven Control)

This demonstrates how to control a running task by updating the database.

### 1. Create a ContinueTask

```bash
poetry run python manage.py create_task --type continue --message "Waiting for signal..."
```

Output will show:
```
Created ContinueTask with ID: 1
  Message: Waiting for signal...
  Continue: False

  To continue the task, run:
    poetry run python manage.py shell -c "from streaming.models import ContinueTask; import asyncio; asyncio.run(ContinueTask.objects.filter(pk=1).aupdate(continue_field=True))"

Starting task...
✓ Task started

Connect to task stream:
  curl http://127.0.0.1:8888/stream/ContinueTask/1
```

### 2. Connect to the Task

```bash
poetry run python stream_client.py ContinueTask 1
```

You'll see the initial event, and the task will wait:
```
[1] Event: STARTED
    Timestamp: 2025-10-14T19:30:00.000000+00:00
    message: Waiting for signal...
    continue: False
```

The task is now polling the database, waiting for `continue_field` to become True.

### 3. Trigger the Task to Continue

In another terminal, update the database:

```bash
poetry run python manage.py shell
```

Then in the Django shell:
```python
from streaming.models import ContinueTask
import asyncio

# Update the continue field
asyncio.run(ContinueTask.objects.filter(pk=1).aupdate(continue_field=True))
exit()
```

Or use the one-liner:
```bash
poetry run python manage.py shell -c "from streaming.models import ContinueTask; import asyncio; asyncio.run(ContinueTask.objects.filter(pk=1).aupdate(continue_field=True))"
```

### 4. Watch the Stream Continue

Back in your stream client terminal, you should see:

```
[2] Event: FINAL
    Timestamp: 2025-10-14T19:30:15.123456+00:00
    message: Continue signal received
    continue: True

[3] Event: COMPLETE
    Timestamp: 2025-10-14T19:30:15.123500+00:00
    message: Task completed successfully
```

## Test with curl

You can also use `curl` to connect to streams:

```bash
# Create a task first
poetry run python manage.py create_task --type example

# Connect with curl
curl http://127.0.0.1:8888/stream/ExampleTask/1
```

Output:
```
event: start
data: {"message": "Task created via management command", "total_steps": 3, "_task_id": 1, "_model": "ExampleTask", "_timestamp": "2025-10-14T19:24:26.897444+00:00"}

event: progress
data: {"step": 1, "total_steps": 3, "message": "Step 1 of 3", "data": "Processing step 1...", "_task_id": 1, "_model": "ExampleTask", "_timestamp": "2025-10-14T19:24:28.898953+00:00"}

...
```

## Test Task Persistence

Tasks continue running even if clients disconnect!

1. Create a task:
   ```bash
   poetry run python manage.py create_task --type example
   ```

2. Connect to it:
   ```bash
   poetry run python stream_client.py ExampleTask 1
   ```

3. Press `Ctrl+C` to disconnect the client after receiving the first event

4. Wait a few seconds (for the task to continue)

5. Reconnect:
   ```bash
   poetry run python stream_client.py ExampleTask 1
   ```

You should receive the latest event immediately, showing the task continued running!

## Test Health Check

```bash
curl http://127.0.0.1:8888/health
```

Should return:
```
OK
```

## Run Automated Tests

Run the full test suite:

```bash
# Run all tests
poetry run python manage.py test

# Run only streaming tests
poetry run python manage.py test streaming.tests.StreamingSystemTests

# Run with verbose output
poetry run python manage.py test --verbosity=2
```

## Troubleshooting

### Server won't start
- Check if port 8888 is already in use: `lsof -i :8888`
- Kill the process or use a different port: `--port 9999`

### Task not found (404)
- Make sure you created the task first
- Check the task ID matches what you're connecting to
- Verify the server is running

### Connection refused
- Make sure the server is running on port 8888
- Check firewall settings

### Events not appearing
- For ContinueTask, make sure you updated the database field
- Check server logs for errors
- Verify task was started (not created with `--no-start`)

## Clean Up

To remove all tasks from the database:

```bash
poetry run python manage.py shell
```

```python
from streaming.models import ExampleTask, ContinueTask

# Delete all tasks
ExampleTask.objects.all().delete()
ContinueTask.objects.all().delete()
```
