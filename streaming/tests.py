import asyncio
import json
import subprocess
import time
from django.test import TransactionTestCase
import httpx
from httpx_sse import aconnect_sse
from streaming.models import ExampleTask, ContinueTask
from streaming.coordinator import coordinator


class StreamingSystemTests(TransactionTestCase):
    

    async def test_example_task_process(self):
        
        task = await ExampleTask.objects.acreate(message="Test message")
        events = []

        
        queue = asyncio.Queue()
        await task.add_client(queue)

        
        task_coro = asyncio.create_task(task.process())

        
        try:
            for _ in range(5):  
                event = await asyncio.wait_for(queue.get(), timeout=10)
                events.append(event)
        except asyncio.TimeoutError:
            pass

        
        await task_coro

        
        self.assertEqual(len(events), 5)

        
        self.assertEqual(events[0]['type'], 'start')
        self.assertEqual(events[0]['data']['message'], "Test message")
        self.assertEqual(events[0]['data']['total_steps'], 3)

        
        for i in range(1, 4):
            self.assertEqual(events[i]['type'], 'progress')
            self.assertEqual(events[i]['data']['step'], i)
            self.assertEqual(events[i]['data']['total_steps'], 3)

        
        self.assertEqual(events[4]['type'], 'complete')
        self.assertEqual(events[4]['data']['message'], 'Task completed successfully')

    async def test_multiple_clients(self):
        
        task = await ExampleTask.objects.acreate(message="Multi-client test")

        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()

        await task.add_client(queue1)
        await task.add_client(queue2)

        
        task_coro = asyncio.create_task(task.process())

        
        event1 = await asyncio.wait_for(queue1.get(), timeout=5)
        event2 = await asyncio.wait_for(queue2.get(), timeout=5)

        
        self.assertEqual(event1['type'], event2['type'])
        self.assertEqual(event1['type'], 'start')
        self.assertEqual(event1['data'], event2['data'])

        
        await task_coro

    async def test_new_client_gets_latest_data(self):
        
        task = await ExampleTask.objects.acreate(message="Late client test")

        queue1 = asyncio.Queue()
        await task.add_client(queue1)

        
        task_coro = asyncio.create_task(task.process())

        
        await asyncio.wait_for(queue1.get(), timeout=5)

        
        await asyncio.sleep(2.5)

        
        queue2 = asyncio.Queue()
        await task.add_client(queue2)

        
        latest_event = await asyncio.wait_for(queue2.get(), timeout=5)
        self.assertEqual(latest_event['type'], 'progress')
        self.assertGreaterEqual(latest_event['data']['step'], 1)

        
        await task_coro

    async def test_coordinator_manages_tasks(self):
        
        task = await ExampleTask.objects.acreate(message="Coordinator test")
        model_name = 'ExampleTask'

        
        await coordinator.start_task(task, model_name)

        
        self.assertTrue(coordinator.is_task_running(model_name, task.pk))

        
        await asyncio.sleep(7)

        
        self.assertFalse(coordinator.is_task_running(model_name, task.pk))

        
        await task.arefresh_from_db()
        self.assertIsNotNone(task.completed_at)

    async def test_task_continues_after_client_disconnect(self):
        
        task = await ExampleTask.objects.acreate(message="Disconnect test")

        queue = asyncio.Queue()
        await task.add_client(queue)

        
        await coordinator.start_task(task, 'ExampleTask')

        
        await asyncio.wait_for(queue.get(), timeout=5)

        
        await task.remove_client(queue)

        
        await asyncio.sleep(7)

        
        await task.arefresh_from_db()
        self.assertIsNotNone(task.completed_at)

    async def test_multiple_clients_with_continue_field(self):
        task = await ContinueTask.objects.acreate(
            message="Multi-client continue test",
            continue_field=False
        )

        
        queue1 = asyncio.Queue()
        await task.add_client(queue1)

        
        await coordinator.start_task(task, 'ContinueTask')

        
        event1_1 = await asyncio.wait_for(queue1.get(), timeout=5)
        self.assertEqual(event1_1['type'], 'started')
        self.assertEqual(event1_1['data']['message'], "Multi-client continue test")
        self.assertEqual(event1_1['data']['continue'], False)

        
        queue2 = asyncio.Queue()
        await task.add_client(queue2)

        
        event2_1 = await asyncio.wait_for(queue2.get(), timeout=5)
        self.assertEqual(event2_1['type'], 'started')
        self.assertEqual(event2_1['data']['message'], "Multi-client continue test")
        self.assertEqual(event2_1['data']['continue'], False)

        
        
        await ContinueTask.objects.filter(pk=task.pk).aupdate(continue_field=True)

        
        
        await asyncio.sleep(0.6)

        
        event1_2 = await asyncio.wait_for(queue1.get(), timeout=5)
        event2_2 = await asyncio.wait_for(queue2.get(), timeout=5)

        
        self.assertEqual(event1_2['type'], 'final')
        self.assertEqual(event1_2['data']['message'], 'Continue signal received')
        self.assertEqual(event1_2['data']['continue'], True)

        self.assertEqual(event2_2['type'], 'final')
        self.assertEqual(event2_2['data']['message'], 'Continue signal received')
        self.assertEqual(event2_2['data']['continue'], True)

        
        event1_3 = await asyncio.wait_for(queue1.get(), timeout=5)
        event2_3 = await asyncio.wait_for(queue2.get(), timeout=5)

        self.assertEqual(event1_3['type'], 'complete')
        self.assertEqual(event1_3['data']['message'], 'Task completed successfully')

        self.assertEqual(event2_3['type'], 'complete')
        self.assertEqual(event2_3['data']['message'], 'Task completed successfully')

        
        await asyncio.sleep(1)

        
        await task.arefresh_from_db()
        self.assertIsNotNone(task.completed_at)


class HTTPSSEEndpointTests(TransactionTestCase):
    

    @classmethod
    def setUpClass(cls):
        
        super().setUpClass()
        
        cls.server_process = subprocess.Popen(
            ['poetry', 'run', 'python', 'manage.py', 'runserver_stream',
             '--host', '127.0.0.1', '--port', '8888'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        time.sleep(3)

    @classmethod
    def tearDownClass(cls):
        
        cls.server_process.terminate()
        cls.server_process.wait(timeout=5)
        super().tearDownClass()

    async def test_http_sse_endpoint(self):
        
        task = await ExampleTask.objects.acreate(message="HTTP SSE test")

        
        await coordinator.start_task(task, 'ExampleTask')

        events = []

        async with httpx.AsyncClient() as client:
            async with aconnect_sse(
                client,
                'GET',
                f'http://127.0.0.1:8888/stream/ExampleTask/{task.pk}'
            ) as event_source:
                async for event in event_source.aiter_sse():
                    data = json.loads(event.data)
                    events.append({
                        'type': event.event,
                        'data': data
                    })

                    
                    if event.event == 'complete':
                        break

        
        self.assertGreaterEqual(len(events), 5)
        self.assertEqual(events[0]['type'], 'start')
        self.assertEqual(events[-1]['type'], 'complete')

    async def test_http_health_check(self):
        
        async with httpx.AsyncClient() as client:
            response = await client.get('http://127.0.0.1:8888/health')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, 'OK')

    async def test_http_404_for_invalid_path(self):
        
        async with httpx.AsyncClient() as client:
            response = await client.get('http://127.0.0.1:8888/invalid')
            self.assertEqual(response.status_code, 404)

    async def test_multiple_concurrent_sse_connections(self):
        
        task = await ExampleTask.objects.acreate(message="Multi-connection test")

        
        await coordinator.start_task(task, 'ExampleTask')

        async def collect_events(client_id):
            events = []
            async with httpx.AsyncClient() as client:
                async with aconnect_sse(
                    client,
                    'GET',
                    f'http://127.0.0.1:8888/stream/ExampleTask/{task.pk}'
                ) as event_source:
                    async for event in event_source.aiter_sse():
                        data = json.loads(event.data)
                        events.append({
                            'type': event.event,
                            'data': data
                        })
                        if event.event == 'complete':
                            break
            return client_id, events

        
        results = await asyncio.gather(
            collect_events(1),
            collect_events(2),
            collect_events(3)
        )

        
        for client_id, events in results:
            self.assertGreaterEqual(len(events), 5, f"Client {client_id} didn't receive enough events")
            self.assertEqual(events[0]['type'], 'start')
            self.assertEqual(events[-1]['type'], 'complete')
