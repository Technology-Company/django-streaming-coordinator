import asyncio
import json
import logging
import os
import django


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from asgineer import to_asgi
from streaming.coordinator import coordinator

logger = logging.getLogger('streaming')


async def stream_handler(request):
    path_parts = request.path.strip('/').split('/')

    if len(path_parts) != 4 or path_parts[0] != 'stream':
        logger.warning(f"Invalid stream path: {request.path}")
        return 404, {}, "Not Found"

    app_name = path_parts[1]
    model_name = path_parts[2]
    try:
        task_id = int(path_parts[3])
    except ValueError:
        logger.warning(f"Invalid task ID in path: {request.path}")
        return 400, {}, "Invalid task ID"

    logger.info(f"Client connecting to stream: {app_name}/{model_name}/{task_id}")

    task_instance = await coordinator.get_task_instance(app_name, model_name, task_id)

    if task_instance is None:
        logger.error(f"Task not found: {app_name}/{model_name}/{task_id}")
        return 404, {}, "Task not found"


    if task_instance.completed_at:
        logger.info(f"Task {app_name}/{model_name}/{task_id} already completed, returning final state")
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
        logger.info(f"Starting event stream for task {app_name}/{model_name}/{task_id}")
        try:
            while True:

                event_data = await client_queue.get()


                event_type = event_data.get('type', 'message')
                data = event_data.get('data', {})


                data['_task_id'] = task_id
                data['_app'] = app_name
                data['_model'] = model_name
                data['_timestamp'] = event_data.get('timestamp')

                logger.debug(f"Streaming event '{event_type}' for task {task_id}: {data}")

                yield f"event: {event_type}\n"
                yield f"data: {json.dumps(data)}\n\n"


                if event_type == 'complete' or event_type == 'error':
                    logger.info(f"Task {task_id} stream ended with event type: {event_type}")
                    break

        except Exception as e:
            logger.error(f"Error in event stream for task {task_id}: {type(e).__name__}: {str(e)}", exc_info=True)
        finally:
            logger.info(f"Client disconnected from task {app_name}/{model_name}/{task_id}")
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
