import json
import logging
from typing import Any, Dict, List, Optional

from aiobotocore.session import get_session

from .models import SQSClient

logger = logging.getLogger(__name__)


class AsyncSQSService:
    _sqs: Any
    _sqs_ctx: Any

    def __init__(self, client: SQSClient) -> None:
        self.client = client

        self._session = get_session()
        self._client_kwargs: Dict[str, Any] = {"region_name": client.region_name}
        if client.aws_access_key_id:
            self._client_kwargs["aws_access_key_id"] = client.aws_access_key_id
        if client.aws_secret_access_key:
            self._client_kwargs["aws_secret_access_key"] = client.aws_secret_access_key
        if client.aws_session_token:
            self._client_kwargs["aws_session_token"] = client.aws_session_token
        if client.endpoint_url:
            self._client_kwargs["endpoint_url"] = client.endpoint_url

        self._sqs_ctx = None
        self._sqs = None

    async def _get_client(self) -> Any:
        if self._sqs is None:
            self._sqs_ctx = self._session.create_client("sqs", **self._client_kwargs)
            self._sqs = await self._sqs_ctx.__aenter__()
        return self._sqs

    async def close(self) -> None:
        if self._sqs_ctx is not None:
            await self._sqs_ctx.__aexit__(None, None, None)
            self._sqs = None
            self._sqs_ctx = None

    # ---------- Receive ----------

    async def receive_messages(
        self,
        queue_url: Optional[str] = None,
        max_number_of_messages: int = 1,
        wait_time_seconds: int = 20,
        visibility_timeout: Optional[int] = None,
        attribute_names: Optional[List[str]] = None,
        message_attribute_names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Receive up to `max_number_of_messages` from the queue.

        SQS caps MaxNumberOfMessages at 10 per API call, so for larger
        batch sizes this loops until the requested count is reached or
        the queue returns no more messages.
        """
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        collected: List[Dict[str, Any]] = []
        remaining = max_number_of_messages

        first_call = True
        while remaining > 0:
            fetch_count = min(remaining, 10)

            params: Dict[str, Any] = {
                "QueueUrl": url,
                "MaxNumberOfMessages": fetch_count,
                "WaitTimeSeconds": wait_time_seconds if first_call else 0,
                "AttributeNames": attribute_names or ["All"],
                "MessageAttributeNames": message_attribute_names or ["All"],
            }
            if visibility_timeout is not None:
                params["VisibilityTimeout"] = visibility_timeout

            response = await sqs.receive_message(**params)
            messages = response.get("Messages", [])
            first_call = False

            if not messages:
                break

            collected.extend(messages)
            remaining -= len(messages)

            if len(messages) < fetch_count:
                break

        return collected

    # ---------- Send ----------

    async def send_message(
        self,
        message_body: Any,
        queue_url: Optional[str] = None,
        delay_seconds: int = 0,
        message_attributes: Optional[Dict[str, Dict]] = None,
        message_group_id: Optional[str] = None,
        message_deduplication_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        body = json.dumps(message_body) if not isinstance(message_body, str) else message_body

        params: Dict[str, Any] = {
            "QueueUrl": url,
            "MessageBody": body,
            "DelaySeconds": delay_seconds,
        }
        if message_attributes:
            params["MessageAttributes"] = message_attributes
        if message_group_id:
            params["MessageGroupId"] = message_group_id
        if message_deduplication_id:
            params["MessageDeduplicationId"] = message_deduplication_id

        return await sqs.send_message(**params)

    async def send_message_batch(
        self,
        entries: List[Dict[str, Any]],
        queue_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send messages in batches of up to 10 (SQS limit).
        Each entry must have at minimum 'Id' and 'MessageBody'.
        """
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        successful = []
        failed = []

        for i in range(0, len(entries), 10):
            chunk = entries[i : i + 10]
            response = await sqs.send_message_batch(QueueUrl=url, Entries=chunk)
            successful.extend(response.get("Successful", []))
            failed.extend(response.get("Failed", []))

        return {"Successful": successful, "Failed": failed}

    # ---------- Delete ----------

    async def delete_message(self, receipt_handle: str, queue_url: Optional[str] = None) -> None:
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        await sqs.delete_message(QueueUrl=url, ReceiptHandle=receipt_handle)

    async def delete_message_batch(
        self,
        entries: List[Dict[str, str]],
        queue_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete messages in batches of up to 10.
        Each entry must have 'Id' and 'ReceiptHandle'.
        """
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        successful = []
        failed = []

        for i in range(0, len(entries), 10):
            chunk = entries[i : i + 10]
            response = await sqs.delete_message_batch(QueueUrl=url, Entries=chunk)
            successful.extend(response.get("Successful", []))
            failed.extend(response.get("Failed", []))

        return {"Successful": successful, "Failed": failed}

    # ---------- Queue Management ----------

    async def list_queues(self, prefix: Optional[str] = None) -> List[str]:
        sqs = await self._get_client()
        params: Dict[str, Any] = {}
        if prefix:
            params["QueueNamePrefix"] = prefix

        response = await sqs.list_queues(**params)
        return response.get("QueueUrls", [])

    async def get_queue_attributes(
        self,
        attribute_names: Optional[List[str]] = None,
        queue_url: Optional[str] = None,
    ) -> Dict[str, str]:
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        response = await sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=attribute_names or ["All"],
        )
        return response.get("Attributes", {})

    async def create_queue(
        self,
        queue_name: str,
        attributes: Optional[Dict[str, str]] = None,
    ) -> str:
        sqs = await self._get_client()
        params: Dict[str, Any] = {"QueueName": queue_name}
        if attributes:
            params["Attributes"] = attributes

        response = await sqs.create_queue(**params)
        return response["QueueUrl"]

    async def delete_queue(self, queue_url: Optional[str] = None) -> None:
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        await sqs.delete_queue(QueueUrl=url)

    async def purge_queue(self, queue_url: Optional[str] = None) -> None:
        url = queue_url or self.client.queue_url
        if not url:
            raise ValueError("queue_url must be provided either in SQSClient or per-call")

        sqs = await self._get_client()
        await sqs.purge_queue(QueueUrl=url)
