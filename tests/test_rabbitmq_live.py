"""Opt-in test; explicit dedicated TEST URL required, never touches production."""

import asyncio
import os
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("rabbitmq_namespace")


@pytest.mark.asyncio
async def test_dedicated_test_vhost_confirms_delivery_and_dead_lettering():
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("TEST_RABBITMQ_URL not set; isolated test vhost required")
    vhost = unquote(urlsplit(url).path.lstrip("/"))
    if not vhost.startswith("test-medical-rag-"):
        pytest.fail("TEST_RABBITMQ_URL must name a dedicated test-medical-rag-* vhost")
    from medical_rag.messaging import AMQPTransport, decode_message

    transport = AMQPTransport(url)
    await transport.connect()
    try:
        task_id = uuid4().hex
        payload = {
            "schema_version": 1,
            "message_id": uuid4().hex,
            "task_type": "document.ingest",
            "task_id": task_id,
            "dispatch_seq": 0,
            "trace_id": task_id,
        }
        await transport.publish(payload)
        queue = transport.topology["queues"]["document.ingest"]
        message = await queue.get(timeout=10)
        assert decode_message(message.body) == payload
        await message.reject(requeue=False)
        from medical_rag.messaging import DEAD_QUEUES

        dead = await transport.channel.get_queue(DEAD_QUEUES["document.ingest"])
        for _ in range(50):
            message = await dead.get(fail=False, timeout=2)
            if message:
                break
            await asyncio.sleep(0.1)
        assert message is not None
        assert decode_message(message.body) == payload
        await message.ack()
        # No purge/delete: caller owns disposal of the isolated broker/vhost.
    finally:
        await transport.close()
