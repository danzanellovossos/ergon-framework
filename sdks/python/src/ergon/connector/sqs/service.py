"""SQS service (client wrapper) for ergon-framework."""

import json
import logging
from typing import Any, Dict, List, Optional

import boto3

from .models import SQSClient

logger = logging.getLogger(__name__)


class SQSService:
    """
    Wrapper around boto3 SQS client.

    Provides simplified receive, send and delete operations aligned with
    the ergon connector contract.
    """

    def __init__(self, client: SQSClient) -> None:
        self.client = client
        self._sqs = self._create_client()

    # ---------- Client ----------

    def _create_client(self):
        kwargs: Dict[str, Any] = {"region_name": self.client.region_name}

        if self.client.aws_access_key_id:
            kwargs["aws_access_key_id"] = self.client.aws_access_key_id
        if self.client.aws_secret_access_key:
            kwargs["aws_secret_access_key"] = self.client.aws_secret_access_key
        if self.client.aws_session_token:
            kwargs["aws_session_token"] = self.client.aws_session_token
        if self.client.endpoint_url:
            kwargs["endpoint_url"] = self.client.endpoint_url

        return boto3.client("sqs", **kwargs)

    # ---------- Receive ----------

    def receive(
        self,
        queue_url: Optional[str] = None,
        max_messages: Optional[int] = None,
        wait_time_seconds: Optional[int] = None,
        visibility_timeout: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Receive messages from an SQS queue.

        Returns a list of dicts, each containing:
        - data: message body (JSON-decoded when possible, raw string otherwise)
        - receipt_handle: handle used to delete the message later
        - message_id: SQS message ID
        - attributes: SQS system attributes
        - message_attributes: custom message attributes
        """
        url = queue_url or self.client.queue_url
        max_msgs = min(max_messages or self.client.max_number_of_messages, 10)
        wait = wait_time_seconds if wait_time_seconds is not None else self.client.wait_time_seconds
        vis = visibility_timeout if visibility_timeout is not None else self.client.visibility_timeout

        logger.info(
            "Recebendo mensagens SQS (queue_url=%s, max=%s, wait=%ss)",
            url, max_msgs, wait,
        )

        response = self._sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=max_msgs,
            WaitTimeSeconds=wait,
            VisibilityTimeout=vis,
            AttributeNames=["All"],
            MessageAttributeNames=["All"],
        )

        raw_messages = response.get("Messages", [])
        if not raw_messages:
            logger.info("Nenhuma mensagem recebida na fila SQS")
            return []

        result: List[Dict[str, Any]] = []
        for msg in raw_messages:
            body = msg.get("Body", "")
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                data = body

            result.append({
                "data": data,
                "receipt_handle": msg["ReceiptHandle"],
                "message_id": msg["MessageId"],
                "attributes": msg.get("Attributes", {}),
                "message_attributes": msg.get("MessageAttributes", {}),
            })

        logger.info("Mensagens recebidas da fila SQS (qtd=%s)", len(result))
        return result

    # ---------- Send ----------

    def send(
        self,
        body: str,
        queue_url: Optional[str] = None,
        message_attributes: Optional[Dict[str, Any]] = None,
        message_group_id: Optional[str] = None,
        message_deduplication_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a single message to an SQS queue.

        Returns the SQS send_message response dict (contains MessageId, etc.).
        """
        url = queue_url or self.client.queue_url

        kwargs: Dict[str, Any] = {
            "QueueUrl": url,
            "MessageBody": body,
        }
        if message_attributes:
            kwargs["MessageAttributes"] = message_attributes
        if message_group_id:
            kwargs["MessageGroupId"] = message_group_id
        if message_deduplication_id:
            kwargs["MessageDeduplicationId"] = message_deduplication_id

        logger.info("Enviando mensagem SQS (queue_url=%s)", url)
        response = self._sqs.send_message(**kwargs)
        logger.info(
            "Mensagem enviada SQS (message_id=%s)",
            response.get("MessageId"),
        )
        return response

    # ---------- Delete (ack equivalent) ----------

    def delete(
        self,
        receipt_handle: str,
        queue_url: Optional[str] = None,
    ) -> None:
        """
        Delete a message from the queue (equivalent to RabbitMQ ack).
        """
        url = queue_url or self.client.queue_url

        logger.info(
            "Deletando mensagem SQS (queue_url=%s, receipt_handle=%s)",
            url, receipt_handle,
        )
        self._sqs.delete_message(
            QueueUrl=url,
            ReceiptHandle=receipt_handle,
        )
        logger.info("Mensagem deletada com sucesso")
