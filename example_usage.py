"""
Example usage of the streaming coordinator with the new features.
"""
import asyncio
import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from typing import AsyncGenerator, Generator
from streaming import get_client
from streaming.coordinator import coordinator


# Example 1: Create a task programmatically
async def example_create_task():
    """Example of creating a task through Django ORM."""
    from tests.models import ExampleTask

    print("Creating task through Django ORM...")
    task = await ExampleTask.objects.acreate(message="Hello from Django ORM!")
    print(f"Task created: ID={task.pk}")

    # Start the task
    await coordinator.start_task(task, "tests", "ExampleTask")
    print(f"Task started!")

    return task.pk


# Example 2: Using the shared httpx client to fetch data
async def example_httpx_usage():
    """Example of using the shared httpx client."""
    from streaming import get_client

    print("\nUsing shared httpx client...")
    client = get_client()

    # Use the shared client to make HTTP requests
    response = await client.async_client.get("https://httpbin.org/json")
    data = response.json()
    print(f"Fetched data: {data}")

    return data


# Example 3: Using httpx in a StreamTask
async def example_httpx_in_task():
    """Example of creating a task that uses httpx to fetch data."""
    from tests.models import HttpxFetchTask

    print("\nCreating HttpxFetchTask...")
    task = await HttpxFetchTask.objects.acreate(url="https://httpbin.org/json")
    print(f"Task created: ID={task.pk}")

    # Start the task
    await coordinator.start_task(task, "tests", "HttpxFetchTask")
    print(f"Task started and will fetch data from API!")

    return task.pk


# Example 4: Using generators in a StreamTask
async def example_async_generator():
    """
    Example of using async generators in a StreamTask.

    In your model, you would define:

    class MyTask(StreamTask):
        async def process(self):
            async def my_generator():
                for i in range(5):
                    await asyncio.sleep(0.1)
                    yield {
                        'step': i,
                        'message': f'Processing step {i}'
                    }

            # Use the built-in generator processor
            return await self.process_generator(my_generator())
    """
    print("\nAsync generator example:")
    print("Define your StreamTask with an async generator like:")
    print("""
    class MyTask(StreamTask):
        async def process(self):
            async def my_generator():
                for i in range(5):
                    yield {'step': i, 'data': f'Step {i}'}

            return await self.process_generator(my_generator())
    """)


def example_sync_generator():
    """
    Example of using sync generators in a StreamTask.

    In your model, you would define:

    class MyTask(StreamTask):
        async def process(self):
            def my_generator():
                for i in range(5):
                    yield {
                        'step': i,
                        'message': f'Processing step {i}'
                    }

            # Use the built-in sync generator processor
            return await self.process_sync_generator(my_generator())
    """
    print("\nSync generator example:")
    print("Define your StreamTask with a sync generator like:")
    print("""
    class MyTask(StreamTask):
        async def process(self):
            def my_generator():
                for i in range(5):
                    yield {'step': i, 'data': f'Step {i}'}

            return await self.process_sync_generator(my_generator())
    """)


# Example 5: Using httpx in a StreamTask to fetch data
async def example_httpx_usage():
    """
    Example of using httpx in a StreamTask to fetch data.

    In your model, you would define:

    import httpx
    from streaming import StreamTask

    class FetchTask(StreamTask):
        url = models.URLField()

        async def process(self):
            async with httpx.AsyncClient() as client:
                response = await client.get(self.url)
                data = response.json()

                # Send progress events
                await self.send_event('progress', {'status': 'fetched', 'size': len(data)})

                # Process the data...
                return data
    """
    print("\nHTTPX usage example:")
    print("Use httpx in your StreamTask like:")
    print("""
    import httpx
    from streaming import StreamTask

    class FetchTask(StreamTask):
        url = models.URLField()

        async def process(self):
            async with httpx.AsyncClient() as client:
                response = await client.get(self.url)
                data = response.json()
                await self.send_event('progress', {'status': 'fetched'})
                return data
    """)


# Example 5: Using async generators in a task
async def example_async_generator_task():
    """Example of creating a task that uses async generators."""
    from tests.models import AsyncGeneratorTask

    print("\nCreating AsyncGeneratorTask...")
    task = await AsyncGeneratorTask.objects.acreate(count=5)
    print(f"Task created: ID={task.pk}")

    # Start the task
    await coordinator.start_task(task, "tests", "AsyncGeneratorTask")
    print(f"Task started and will yield 5 progress updates!")

    return task.pk


# Example 6: Using sync generators in a task
async def example_sync_generator_task():
    """Example of creating a task that uses sync generators."""
    from tests.models import SyncGeneratorTask

    print("\nCreating SyncGeneratorTask...")
    task = await SyncGeneratorTask.objects.acreate(items=["apple", "banana", "cherry"])
    print(f"Task created: ID={task.pk}")

    # Start the task
    await coordinator.start_task(task, "tests", "SyncGeneratorTask")
    print(f"Task started and will process 3 items!")

    return task.pk


async def main():
    """Run all examples."""
    print("=" * 60)
    print("Django Streaming Coordinator - Usage Examples")
    print("=" * 60)

    # Example 1: Create task programmatically
    await example_create_task()

    # Give tasks time to run
    await asyncio.sleep(0.5)

    # Example 2: Use shared httpx client
    await example_httpx_usage()

    # Example 3: Task using httpx
    await example_httpx_in_task()

    # Give task time to run
    await asyncio.sleep(1)

    # Example 4: Async generator
    await example_async_generator()

    # Example 5: Sync generator
    example_sync_generator()

    # Example 6: Async generator task
    await example_async_generator_task()

    # Give task time to run
    await asyncio.sleep(1)

    # Example 7: Sync generator task
    await example_sync_generator_task()

    # Give task time to run
    await asyncio.sleep(1)

    print("\n" + "=" * 60)
    print("Examples completed!")
    print("To view task streams, connect to: http://127.0.0.1:8888/stream/tests/ModelName/task_id")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
