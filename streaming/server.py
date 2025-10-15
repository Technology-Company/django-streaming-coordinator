import asyncio
import json
import os
import django


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from asgineer import to_asgi
from streaming.coordinator import coordinator


async def stream_handler(request):
    path_parts = request.path.strip('/').split('/')

    if len(path_parts) != 4 or path_parts[0] != 'stream':
        return 404, {}, "Not Found"

    app_name = path_parts[1]
    model_name = path_parts[2]
    try:
        task_id = int(path_parts[3])
    except ValueError:
        return 400, {}, "Invalid task ID"


    task_instance = await coordinator.get_task_instance(app_name, model_name, task_id)

    if task_instance is None:
        return 404, {}, "Task not found"


    if task_instance.completed_at:

        headers = {
            'Content-Type': 'application/json',
        }
        response_data = {
            'status': 'completed',
            'final_value': task_instance.final_value,
            'completed_at': task_instance.completed_at.isoformat(),
        }
        return 200, headers, json.dumps(response_data)


    client_queue = asyncio.Queue()


    await task_instance.add_client(client_queue)

    async def event_generator():
        
        try:
            while True:
                
                event_data = await client_queue.get()

                
                event_type = event_data.get('type', 'message')
                data = event_data.get('data', {})


                data['_task_id'] = task_id
                data['_app'] = app_name
                data['_model'] = model_name
                data['_timestamp'] = event_data.get('timestamp')

                
                yield f"event: {event_type}\n"
                yield f"data: {json.dumps(data)}\n\n"

                
                if event_type == 'complete' or event_type == 'error':
                    break

        finally:
            
            await task_instance.remove_client(client_queue)

    
    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
    }

    return 200, headers, event_generator()


async def health_check(request):
    
    if request.path == '/health':
        return 200, {'Content-Type': 'text/plain'}, "OK"
    return None


async def main_handler(request):
    
    
    health_response = await health_check(request)
    if health_response:
        return health_response

    
    if request.path.startswith('/stream/'):
        return await stream_handler(request)

    return 404, {}, "Not Found"



app = to_asgi(main_handler)
