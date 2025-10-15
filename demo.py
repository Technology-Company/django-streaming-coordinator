import os
import django
import asyncio


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from tests.models import ExampleTask
from streaming.coordinator import coordinator


async def main():
    
    print("=" * 60)
    print("Django Streaming Coordinator Demo")
    print("=" * 60)

    
    print("\n1. Creating task...")
    task = await ExampleTask.objects.acreate(
        message="Demo: Hello from the streaming task system!"
    )
    print(f"   ✓ Task created with ID: {task.pk}")

    
    print("\n2. Connecting client...")
    event_queue = asyncio.Queue()
    await task.add_client(event_queue)
    print("   ✓ Client connected")


    print("\n3. Starting task...")
    await coordinator.start_task(task, 'tests', 'ExampleTask')
    print("   ✓ Task started")

    print("\n4. Receiving events:")
    print("   " + "-" * 56)

    
    event_count = 0
    while True:
        try:
            event = await asyncio.wait_for(event_queue.get(), timeout=10)
            event_count += 1

            event_type = event['type']
            data = event['data']
            timestamp = event['timestamp']

            print(f"   [{event_count}] {event_type.upper()}")
            print(f"       Time: {timestamp}")

            for key, value in data.items():
                if not key.startswith('_'):
                    print(f"       {key}: {value}")

            print()

            
            if event_type in ('complete', 'error'):
                break

        except asyncio.TimeoutError:
            print("   Timeout waiting for events")
            break

    
    await task.remove_client(event_queue)

    
    print("\n5. Waiting for task to complete...")
    await asyncio.sleep(1)

    
    await task.arefresh_from_db()

    print("   " + "-" * 56)
    print(f"   ✓ Task completed at: {task.completed_at}")
    print(f"   ✓ Total events received: {event_count}")

    print("\n" + "=" * 60)
    print("Demo completed successfully!")
    print("\nTo test with HTTP:")
    print(f"  1. Start server: poetry run python manage.py runserver_stream --port 8888")
    print(f"  2. Connect: curl http://127.0.0.1:8888/stream/tests/ExampleTask/{task.pk}")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
