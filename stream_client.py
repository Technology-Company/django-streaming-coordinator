import sys
import asyncio
import json
import httpx
from httpx_sse import aconnect_sse


async def stream_task(app_name: str, model_name: str, task_id: int, host: str = "127.0.0.1", port: int = 8888):

    url = f"http://{host}:{port}/stream/{app_name}/{model_name}/{task_id}"

    print("=" * 60)
    print(f"Connecting to: {url}")
    print("=" * 60)
    print()

    try:
        async with httpx.AsyncClient() as client:
            async with aconnect_sse(client, 'GET', url) as event_source:
                event_count = 0

                async for event in event_source.aiter_sse():
                    event_count += 1
                    data = json.loads(event.data)

                    print(f"[{event_count}] Event: {event.event.upper()}")
                    print(f"    Timestamp: {data.get('_timestamp', 'N/A')}")

                    
                    for key, value in data.items():
                        if not key.startswith('_'):
                            print(f"    {key}: {value}")

                    print()

                    
                    if event.event in ('complete', 'error', 'timeout'):
                        print("=" * 60)
                        print(f"Stream ended with: {event.event.upper()}")
                        print(f"Total events received: {event_count}")
                        print("=" * 60)
                        break

    except httpx.ConnectError:
        print(f"ERROR: Could not connect to {url}")
        print("Make sure the server is running:")
        print("  poetry run python manage.py runserver_stream --port 8888")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"ERROR: HTTP {e.response.status_code}")
        if e.response.status_code == 404:
            print(f"Task not found: {app_name}/{model_name}/{task_id}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nConnection closed by user")
        sys.exit(0)


def main():
    if len(sys.argv) < 4:
        print("Usage: poetry run python stream_client.py <app_name> <model_name> <task_id>")
        print()
        print("Examples:")
        print("  poetry run python stream_client.py tests ExampleTask 1")
        print("  poetry run python stream_client.py tests ContinueTask 2")
        sys.exit(1)

    app_name = sys.argv[1]
    model_name = sys.argv[2]
    task_id = int(sys.argv[3])

    asyncio.run(stream_task(app_name, model_name, task_id))


if __name__ == '__main__':
    main()
