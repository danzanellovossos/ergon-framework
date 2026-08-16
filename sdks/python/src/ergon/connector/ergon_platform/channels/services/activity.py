from contextlib import suppress
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from ....transaction import Transaction
from ..adapters import ActivityAdapter
from ..models import ErgonPlatformChannelsConsumerConfig
from .records import SdkRecord


class ChannelsActivityService:
    """Activity history, inbox claims, and lease settlement."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def fetch_activity_events(
        self,
        company_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch the activity events."""
        response = self.client.channels.company_activity(
            company_id=company_id,
            **self._page_query(params, limit=limit, offset=offset),
        )
        events = SdkRecord.items(
            response,
            keys=["items", "data", "results"],
        )
        return [ActivityAdapter.to_transaction(event, source="activity") for event in events]

    def fetch_inbox_events(
        self,
        config_id: str,
        address_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch the inbox events."""
        response = self.client.channels.configs.activity(
            config_id,
            **self._page_query(
                params,
                limit=limit,
                offset=offset,
                address_id=address_id,
            ),
        )
        events = SdkRecord.items(
            response,
            keys=["items", "data", "results"],
        )
        return [
            ActivityAdapter.to_transaction(
                event,
                source="config_activity",
            )
            for event in events
        ]

    def claim_inbox_transactions(
        self,
        config_id: str,
        address_id: str,
        config: ErgonPlatformChannelsConsumerConfig,
        limit: int,
        seen_ids: Optional[set[str]] = None,
    ) -> List[Transaction]:
        """Claim a batch and release every lease if pagination/filtering fails."""
        leased: List[Transaction] = []
        try:
            return self._claim_inbox_transactions(
                config_id=config_id,
                address_id=address_id,
                config=config,
                limit=limit,
                seen_ids=seen_ids,
                leased=leased,
            )
        except Exception:
            self.release_claims(
                config_id,
                leased,
                delay_seconds=config.nack_delay_seconds,
            )
            if seen_ids is not None:
                for transaction in leased:
                    if transaction.id:
                        seen_ids.discard(transaction.id)
            raise

    def _claim_inbox_transactions(
        self,
        config_id: str,
        address_id: str,
        config: ErgonPlatformChannelsConsumerConfig,
        limit: int,
        seen_ids: Optional[set[str]],
        leased: List[Transaction],
    ) -> List[Transaction]:
        """Claim the inbox transactions."""
        subscription_id = config.resolved_subscription_id(
            config_id,
            address_id,
        )
        cursor: Optional[str] = None
        accepted: List[Transaction] = []
        activity_filter = config.effective_activity_filter()

        claim = self._activity_claim()

        while len(accepted) < limit:
            response = claim(
                config_id,
                subscription_id=subscription_id,
                address_id=address_id,
                consumer_id=config.consumer_id,
                limit=min(
                    config.claim_page_size,
                    max(1, limit - len(accepted)),
                ),
                visibility_timeout_seconds=config.visibility_timeout_seconds,
                cursor=cursor,
                idempotency_key=str(uuid4()),
            )
            items = SdkRecord.items(response, keys=["items"])
            if not items:
                break
            for item in items:
                transaction = ActivityAdapter.claimed_transaction(item)
                leased.append(transaction)
                matches = ActivityAdapter.belongs_to_address(
                    transaction,
                    address_id,
                )
                matches = matches and activity_filter.matches(transaction)
                duplicate = bool(seen_ids is not None and transaction.id and transaction.id in seen_ids)
                if not matches or duplicate:
                    self.ack_inbox_event(config_id, transaction)
                    leased.remove(transaction)
                    continue
                accepted.append(transaction)
                if seen_ids is not None and transaction.id:
                    seen_ids.add(transaction.id)
                if len(accepted) >= limit:
                    break
            cursor_value = SdkRecord(response).get("next_cursor")
            cursor = str(cursor_value) if cursor_value else None
            if cursor is None:
                break
        return accepted

    def _activity_claim(self) -> Callable[..., Any]:
        """Return the SDK claim operation after validating its availability."""
        claim = getattr(
            self.client.channels.configs,
            "activity_claim",
            None,
        )
        if not callable(claim):
            raise RuntimeError("ergon-platform-sdk>=0.2.0 is required: channels.configs.activity_claim is unavailable")
        return claim

    def get_activity_count(
        self,
        company_id: Optional[str] = None,
        **params: Any,
    ) -> int:
        """Get the activity count."""
        query = {**params, "limit": 1, "page": 1}
        response = self.client.channels.company_activity(
            company_id=company_id,
            **query,
        )
        return SdkRecord.total(response)

    def get_inbox_events_count(
        self,
        config_id: str,
        address_id: Optional[str] = None,
        **params: Any,
    ) -> int:
        """Get the inbox events count."""
        query: Dict[str, Any] = {**params, "limit": 1, "page": 1}
        if address_id:
            query["address_id"] = address_id
        response = self.client.channels.configs.activity(config_id, **query)
        return SdkRecord.total(response)

    def get_activity_event(
        self,
        event_id: str,
        company_id: Optional[str] = None,
        **params: Any,
    ) -> Transaction:
        """Get the activity event."""
        event = self.client.channels.company_activity_event(
            company_id=company_id,
            event_id=event_id,
            **params,
        )
        return ActivityAdapter.to_transaction(event, source="activity")

    def get_inbox_event(
        self,
        config_id: str,
        event_id: str,
        **params: Any,
    ) -> Transaction:
        """Get the inbox event."""
        event = self.client.channels.configs.activity_event(
            config_id,
            event_id,
            **params,
        )
        return ActivityAdapter.to_transaction(
            event,
            source="config_activity",
        )

    @staticmethod
    def finalize_fetched_transactions(
        transactions: List[Transaction],
        config: ErgonPlatformChannelsConsumerConfig,
        seen_ids: Optional[set[str]] = None,
    ) -> List[Transaction]:
        """Finalize the fetched transactions."""
        return ActivityAdapter.finalize_fetch(
            transactions,
            config,
            seen_ids=seen_ids,
        )

    def ack_inbox_event(
        self,
        config_id: str,
        transaction: Transaction,
    ) -> Any:
        """Ack the inbox event."""
        delivery = ActivityAdapter.delivery(transaction)
        return self.client.channels.configs.activity_ack(
            config_id,
            transaction.id,
            subscription_id=delivery["subscription_id"],
            lease_token=delivery["lease_token"],
        )

    def nack_inbox_event(
        self,
        config_id: str,
        transaction: Transaction,
        requeue: bool = True,
        delay_seconds: int = 0,
    ) -> Any:
        """Nack the inbox event."""
        delivery = ActivityAdapter.delivery(transaction)
        return self.client.channels.configs.activity_nack(
            config_id,
            transaction.id,
            subscription_id=delivery["subscription_id"],
            lease_token=delivery["lease_token"],
            requeue=requeue,
            delay_seconds=delay_seconds,
        )

    def release_claims(
        self,
        config_id: str,
        transactions: List[Transaction],
        delay_seconds: int,
    ) -> None:
        """Best-effort requeue of a batch that cannot be handed to the task."""
        for transaction in transactions:
            with suppress(Exception):
                self.nack_inbox_event(
                    config_id,
                    transaction,
                    requeue=True,
                    delay_seconds=delay_seconds,
                )

    @staticmethod
    def _page_query(
        params: Dict[str, Any],
        limit: Optional[int],
        offset: int,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Page query."""
        query = {key: value for key, value in extra.items() if value is not None}
        query.update(params)
        if limit is not None:
            query.setdefault("limit", limit)
        page_size = limit or 50
        query.setdefault("page", (offset // page_size) + 1)
        return query
