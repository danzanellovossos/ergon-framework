import json
import os
from logging import getLogger
from typing import Any, Dict, List, Optional

import httpx

from ergon.task.helpers import run_fn_async
from ergon.task.policies import RetryPolicy

from .models import (
    CreateCardInput,
    FieldFilter,
    FieldFilterOperator,
    PipefyClient,
)

logger = getLogger(__name__)

default_retry = RetryPolicy(max_attempts=5, backoff=1, backoff_multiplier=2, backoff_cap=10)


class AsyncPipefyService:
    def __init__(self, client: PipefyClient):
        logger.info("Initializing AsyncPipefyService")

        self.client = client
        self.endpoint = client.endpoint
        self.timeout_sec = client.timeout_sec

        self._token: str | None = None
        self._http: httpx.AsyncClient | None = None
        self._after_cursor = None
        self._has_next_page = False

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self.timeout_sec)
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    # ---------------------------------------------------------
    # AUTHENTICATION
    # ---------------------------------------------------------

    def get_access_token(self) -> str | None:
        return self._token

    @run_fn_async(retry=default_retry, trace_name="AsyncPipefyService.authenticate")
    async def authenticate(self) -> None:
        logger.info("Authenticating with Pipefy (async)")
        http = await self._get_http()
        resp = await http.post(
            self.client.oauth_token_url,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client.client_id,
                "client_secret": self.client.client_secret,
            },
            timeout=30,
        )

        resp.raise_for_status()
        data = resp.json()
        self._token = data.get("access_token")

        logger.info("Authenticated successfully (async)")

    # ---------------------------------------------------------
    # GENERIC GRAPHQL
    # ---------------------------------------------------------
    async def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        if not self._token:
            raise ValueError("Authentication required")

        http = await self._get_http()
        payload = {"query": query, "variables": variables}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        resp = await http.post(self.endpoint, json=payload, headers=headers)

        logger.debug(f"GraphQL query response: {resp.status_code}")

        if resp.status_code == 401:
            logger.debug("Unauthorized. Re-authenticating with Pipefy (async)")
            await self.authenticate()
            headers["Authorization"] = f"Bearer {self._token}"
            resp = await http.post(self.endpoint, json=payload, headers=headers)
            logger.debug("Re-authenticated successfully (async)")

        resp.raise_for_status()
        data = resp.json()

        logger.debug("GraphQL query completed successfully")
        return data.get("data", {})

    @run_fn_async(retry=default_retry)
    async def get_pipe_fields(
        self,
        pipe_id: str,
        response_fields: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not pipe_id:
            raise RuntimeError("pipe_id must be provided")

        if response_fields is None:
            response_fields = """
                id
                name
                phases {
                    id
                    name
                    fields {
                        id
                        label
                    }
                }
            """

        query = f"""
        query GetPipeFields($pipeId: ID!) {{
            pipe(id: $pipeId) {{
                {response_fields}
            }}
        }}
        """

        data = await self._graphql(query, {"pipeId": pipe_id})

        pipe = data.get("pipe") or {}
        phases = pipe.get("phases") or []

        total_fields = sum(len(p.get("fields", [])) for p in phases)
        logger.info(
            "Retrieved %d phases and %d total fields from pipe %s",
            len(phases),
            total_fields,
            pipe_id,
        )

        result = []
        for phase in phases:
            for f in phase.get("fields", []):
                result.append(
                    {
                        "phase_id": phase.get("id"),
                        "phase_name": phase.get("name"),
                        "id": f.get("id"),
                        "label": f.get("label"),
                    }
                )

        return result

    # -------------------------------------------------------------
    @run_fn_async(retry=default_retry)
    async def get_pipe_start_form_fields(
        self,
        pipe_id: str,
        response_fields: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not pipe_id:
            raise RuntimeError("pipe_id must be provided")

        if response_fields is None:
            response_fields = """
                id
                name
                start_form_fields {
                    id
                    label
                }
            """

        query = f"""
        query GetStartFormFields($pipeId: ID!) {{
            pipe(id: $pipeId) {{
                {response_fields}
            }}
        }}
        """

        data = await self._graphql(query, {"pipeId": pipe_id})

        pipe = data.get("pipe") or {}
        fields = pipe.get("start_form_fields") or []

        result = [
            {
                "phase_id": pipe.get("id"),
                "phase_name": pipe.get("name"),
                "id": f.get("id"),
                "label": f.get("label"),
            }
            for f in fields
        ]

        logger.info(
            "Retrieved %d start_form_fields from pipe %s (%s)",
            len(fields),
            pipe_id,
            pipe.get("name"),
        )

        return result

    # ---------------------------------------------------------
    # CARD QUERIES
    # ---------------------------------------------------------

    @run_fn_async(retry=default_retry)
    async def get_card_by_id(
        self,
        card_id: str,
        response_fields: Optional[str] = None,
    ) -> Dict:
        if response_fields is None:
            response_fields = """
                id
                title
                fields {
                    field { id }
                    name
                    value
                }
                current_phase { id name }
                updated_at
            """

        query = f"""
        query GetCard($id: ID!) {{
            card(id: $id) {{
                {response_fields}
            }}
        }}
        """

        data = await self._graphql(query, {"id": card_id})
        return data.get("card", {})

    # ---------------------------------------------------------

    @run_fn_async(retry=default_retry)
    async def get_next_card(
        self,
        phase_id: str,
        field_filters: Optional[List[FieldFilter]] = None,
        batch_size: int = 1,
        response_fields: Optional[str] = None,
    ) -> Optional[List[Dict]]:
        if response_fields is None:
            response_fields = """
                id
                title
                fields {
                    field { id }
                    name
                    value
                }
                current_phase { id name }
                updated_at
            """

        query = f"""
        query GetCardsFromPhase($phaseId: ID!, $after: String, $batch_size: Int!) {{
            phase(id: $phaseId) {{
                id
                name
                cards(first: $batch_size, after: $after) {{
                    edges {{
                        node {{
                            {response_fields}
                        }}
                    }}
                    pageInfo {{
                        hasNextPage
                        endCursor
                    }}
                }}
            }}
        }}
        """

        data = await self._graphql(
            query,
            {"phaseId": phase_id, "after": self._after_cursor, "batch_size": batch_size},
        )

        phase = data.get("phase", {})
        if not phase:
            return None

        cards = phase.get("cards", {}).get("edges", [])
        if not cards:
            self._after_cursor = None
            self._has_next_page = False
            return None

        page_info = phase.get("cards", {}).get("pageInfo", {})
        self._after_cursor = page_info.get("endCursor")
        self._has_next_page = page_info.get("hasNextPage")

        filtered = []
        for edge in cards:
            card = edge.get("node", {})
            if self.__apply_client_side_filter(card, field_filters):
                filtered.append(card)

        return filtered or None

    @run_fn_async(retry=default_retry)
    async def search_cards_by_field(
        self,
        pipe_id: str,
        field_id: str,
        field_value: str,
        response_fields: Optional[str] = None,
    ) -> List[Dict]:
        if not pipe_id:
            raise RuntimeError("pipe_id must be provided")
        if not field_id:
            raise RuntimeError("field_id must be provided")

        if response_fields is None:
            response_fields = """
                id
                title
                updated_at
                current_phase { id name }
                fields {
                    field { id }
                    name
                    value
                }
            """

        query = f"""
        query FindCards($pipeId: ID!, $fieldId: String!, $fieldValue: String!) {{
            findCards(
                pipeId: $pipeId,
                search: {{ fieldId: $fieldId, fieldValue: $fieldValue }}
            ) {{
                edges {{
                    node {{
                        {response_fields}
                    }}
                }}
            }}
        }}
        """

        variables = {
            "pipeId": str(pipe_id),
            "fieldId": field_id,
            "fieldValue": field_value,
        }

        data = await self._graphql(query, variables)
        edges = ((data.get("findCards") or {}).get("edges")) or []

        cards = [edge.get("node", {}) for edge in edges]

        logger.info(
            "Found %d cards in pipe %s where field %s == %s",
            len(cards),
            pipe_id,
            field_id,
            field_value,
        )

        return cards

    # ---------------------------------------------------------
    # CREATE CARD
    # ---------------------------------------------------------

    @run_fn_async(retry=default_retry)
    async def create_card(
        self,
        card: CreateCardInput,
        response_fields: Optional[str] = None,
    ) -> Dict:
        if response_fields is None:
            response_fields = """
                id
                title
                current_phase { id name }
                fields { name value field { id type } }
                created_at
            """

        mutation = f"""
        mutation CreateCard($input: CreateCardInput!) {{
            createCard(input: $input) {{
                card {{
                    {response_fields}
                }}
            }}
        }}
        """

        variables = {"input": card.model_dump()}
        data = await self._graphql(mutation, variables)

        return (data.get("createCard") or {}).get("card") or {}

    # ---------------------------------------------------------
    # FIELD HELPERS
    # ---------------------------------------------------------

    def get_field_value_by_name(self, card: Dict, field_name: str) -> Any:
        for field in card.get("fields", []):
            if field.get("name") == field_name:
                try:
                    is_list = json.loads(field.get("value"))
                    if isinstance(is_list, list):
                        if is_list:
                            return is_list
                        else:
                            return None
                    else:
                        return field.get("value")
                except Exception:
                    return field.get("value")
        return None

    def get_field_value_by_id(self, card: Dict, field_id: str) -> Any:
        for field in card.get("fields", []):
            if field.get("field", {}).get("id", {}) == field_id:
                try:
                    is_list = json.loads(field.get("value"))
                    if isinstance(is_list, list):
                        if is_list:
                            return is_list
                        else:
                            return None
                    else:
                        return field.get("value")
                except Exception:
                    return field.get("value")
        return None

    # ---------------------------------------------------------
    # CARD OPERATIONS
    # ---------------------------------------------------------
    @run_fn_async(retry=default_retry)
    async def move_card_to_phase(
        self,
        card_id: str,
        phase_id: str,
        response_fields: str | None = None,
    ):
        if not response_fields:
            response_fields = """
                card {
                    id
                    title
                    current_phase {
                        id
                        name
                    }
                }
            """

        mutation = f"""
        mutation MoveCardToPhase($input: MoveCardToPhaseInput!) {{
            moveCardToPhase(input: $input) {{
                {response_fields}
            }}
        }}
        """

        variables = {"input": {"card_id": card_id, "destination_phase_id": phase_id}}

        data = await self._graphql(mutation, variables)
        result = data.get("moveCardToPhase")

        return result is not None

    # ---------------------------------------------------------
    @run_fn_async(retry=default_retry)
    async def update_card_fields_by_id(
        self,
        card_id: str,
        fields: Dict[str, Any],
        response_fields: Optional[str] = None,
    ) -> Dict:
        if not response_fields:
            response_fields = """
                ... on Card {
                    id
                    title
                    fields { name value }
                    current_phase { id name }
                }
            """

        mutation = f"""
        mutation UpdateFieldsValues($input: UpdateFieldsValuesInput!) {{
            updateFieldsValues(input: $input) {{
                success
                updatedNode {{
                    {response_fields}
                }}
                userErrors {{
                    field
                    message
                }}
            }}
        }}
        """

        field_attrs = [{"fieldId": fid, "value": value} for fid, value in fields.items()]

        variables = {
            "input": {
                "nodeId": card_id,
                "values": field_attrs,
            }
        }

        data = await self._graphql(mutation, variables)
        result = data.get("updateFieldsValues") or {}

        if result.get("userErrors"):
            raise RuntimeError(f"Pipefy update failed: {result['userErrors']}")

        return result.get("updatedNode") or {}

    @run_fn_async(retry=default_retry)
    async def update_card_field_by_id(
        self,
        card_id: str,
        field_id: str,
        new_value: str,
    ) -> bool:
        mutation = """
        mutation UpdateCardField($input: UpdateCardFieldInput!) {
            updateCardField(input: $input) {
                success
            }
        }
        """

        variables = {
            "input": {
                "card_id": card_id,
                "field_id": field_id,
                "new_value": new_value,
            }
        }

        data = await self._graphql(mutation, variables)
        return (data.get("updateCardField") or {}).get("success", False)

    # ---------------------------------------------------------
    # FILTERS
    # ---------------------------------------------------------

    def __apply_client_side_filter(
        self,
        card: Dict,
        field_filters: Optional[List[FieldFilter]],
    ) -> Optional[Dict]:
        if not field_filters:
            return card

        card_fields = card.get("fields", [])

        for filter in field_filters:
            value = next(
                (f.get("value") for f in card_fields if f.get("name") == filter.field),
                None,
            )

            if value is None:
                return None

            if filter.operator == FieldFilterOperator.EQUAL:
                if value != filter.value:
                    return None

        return card

    @run_fn_async(retry=default_retry)
    async def presign_url(
        self,
        org_id: str,
        file_path: str,
        response_fields: Optional[str] = None,
    ) -> Dict:
        if not org_id:
            raise RuntimeError("org_id must be provided")

        if not file_path:
            raise RuntimeError("file_path must be provided")

        if response_fields is None:
            response_fields = "url"

        mutation = f"""
        mutation CreatePresignedUrl($input: CreatePresignedUrlInput!) {{
            createPresignedUrl(input: $input) {{
                {response_fields}
            }}
        }}
        """

        variables = {
            "input": {
                "organizationId": org_id,
                "fileName": os.path.basename(str(file_path)),
            }
        }

        data = await self._graphql(mutation, variables)
        result = data.get("createPresignedUrl") or {}

        logger.debug(
            "Received presigned URL for '%s' in org '%s': %s",
            os.path.basename(file_path),
            org_id,
            result.get("url"),
        )

        return result

    @run_fn_async(retry=default_retry)
    async def upload_file(
        self,
        presigned_url: Dict,
        file_path: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        url = presigned_url.get("url")
        if not url:
            raise RuntimeError("Invalid presigned_url: missing 'url'")

        if not file_path:
            raise RuntimeError("file_path must be provided")

        logger.debug("Uploading file '%s' → %s (async)", file_path, url)

        http = await self._get_http()
        with open(file_path, "rb") as f:
            content = f.read()

        resp = await http.put(
            url,
            content=content,
            headers={"Content-Type": content_type},
            timeout=60,
        )
        resp.raise_for_status()

        logger.info("Successfully uploaded file '%s' (async).", file_path)

        clean_url = url.split("?")[0]
        s3_key = clean_url.split("amazonaws.com/")[-1]

        logger.debug("Resolved S3 key after upload: %s", s3_key)

        return s3_key

    @run_fn_async(retry=default_retry)
    async def get_database_record_by_title(
        self,
        database_id: str,
        title: str,
        limit: int = 100,
        response_fields: Optional[str] = None,
    ) -> List[Dict]:
        if response_fields is None:
            response_fields = """
				id
				title
				record_fields {
					name
					value
					field { id }
				}
			"""

        query = f"""
			query GetDatabaseRecords($databaseId: ID!, $first: Int!, $title: String!) {{
				table_records(
					table_id: $databaseId,
					first: $first,
					search: {{ title: $title }}
				) {{
					edges {{
						node {{
							{response_fields}
						}}
					}}
				}}
			}}
		"""

        variables = {
            "databaseId": str(database_id),
            "first": limit,
            "title": title,
        }

        try:
            data = await self._graphql(query, variables)

            edges = (data.get("table_records") or {}).get("edges", [])
            records = [edge.get("node", {}) for edge in edges]

            logger.info(
                "Retrieved %d database records from database %s matching title '%s'",
                len(records),
                database_id,
                title,
            )

            return records

        except Exception as e:
            logger.error(
                f"Error fetching database records for database {database_id} and title '{title}': {e}",
                exc_info=True,
            )
            return []

    @run_fn_async(retry=default_retry)
    async def download_card_attachments(
        self,
        field_id: str,
        card_id: Optional[str] = None,
        card: Optional[dict] = None,
        output_dir: str = "attachments",
    ) -> List[str]:
        if not card and not card_id:
            raise Exception("Either card or card_id must be passed as parameter")

        if card_id:
            card = await self.get_card_by_id(card_id)

        if not card:
            raise Exception("Could not resolve card")

        attachments = self.get_field_value_by_id(card, field_id)

        if not attachments:
            logger.warning("No attachments found for field '%s' in card %s", field_id, card_id)
            return []

        if isinstance(attachments, str):
            attachments = [attachments]
        elif not isinstance(attachments, list):
            logger.error("Unexpected attachments format: %s", type(attachments))
            return []

        os.makedirs(output_dir, exist_ok=True)
        saved_files = []

        http = await self._get_http()
        for i, url in enumerate(attachments):
            clean_url = url.split("?")[0]
            original_filename = clean_url.split("/")[-1]

            filename = f"{i}_{original_filename}"
            file_path = os.path.join(output_dir, filename)

            logger.info("Downloading attachment %d to %s (async)", i, file_path)

            try:
                response = await http.get(url, timeout=60)
                response.raise_for_status()

                with open(file_path, "wb") as f:
                    f.write(response.content)

                saved_files.append(file_path)
            except Exception as e:
                logger.error("Failed to download %s: %s", url, e)

        return saved_files

    @run_fn_async(retry=default_retry)
    async def _prepare_and_upload_file(self, presigned_url: Dict, file_path: str) -> str:
        url: str = presigned_url["url"]
        http = await self._get_http()

        await http.put(
            url,
            content=b"BINARY_DATA",
            headers={"Content-Type": "application/pdf"},
        )

        logger.debug("Prepared presigned URL (async)")
        logger.debug("Uploading file %s to presigned URL (async)", file_path)
        with open(file_path, "rb") as f:
            content = f.read()

        resp = await http.put(url, content=content)
        resp.raise_for_status()
        logger.debug("Uploaded file %s to presigned URL (async)", file_path)

        clean_url = url.split("?")[0]
        return clean_url.split("amazonaws.com/")[-1]

    @run_fn_async(retry=default_retry)
    async def attach_file_to_card(
        self,
        card_id: str,
        field_id: str,
        file_paths: List[str],
        org_id: str,
    ) -> bool:
        parsed_urls: List[str] = []
        for file_path in file_paths:
            presigned_url = await self.presign_url(org_id, file_path)

            if not presigned_url:
                raise RuntimeError("Failed to get presigned URL from Pipefy")

            parsed_url = await self._prepare_and_upload_file(presigned_url, file_path)
            parsed_urls.append(parsed_url)

        result = await self.update_card_field_by_id(
            card_id=card_id, field_id=field_id, new_value=json.dumps(parsed_urls)
        )

        if result:
            logger.info(
                "Updated card %s field %s with uploaded file (async).",
                card_id,
                field_id,
            )
        else:
            logger.error(
                "Failed to update card %s field %s with uploaded file (async).",
                card_id,
                field_id,
            )

        return result
