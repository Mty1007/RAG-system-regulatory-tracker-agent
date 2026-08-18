"""AstraDB (IBM DataStax) store for Docling bbox/layout data.

Each document element produced by Docling is persisted as a row keyed by
``(doc_id, element_id)``.  The layout data (coordinates, page number, element
type, and extracted text) lives here as structured columns — not as a JSON
sidecar object in COS.

Required environment variables
-------------------------------
ASTRA_DB_APPLICATION_TOKEN   DataStax token  (AstraCS:…)
ASTRA_DB_API_ENDPOINT        REST endpoint   https://<db-id>-<region>.apps.astra.datastax.com
ASTRA_DB_KEYSPACE            Keyspace name   (optional, defaults to "default_keyspace")

The table ``layout_elements`` is created automatically on first use if it does
not already exist.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from cassandra.cluster import Cluster, Session
from cassandra.auth import PlainTextAuthProvider
from cassandra.policies import DCAwareRoundRobinPolicy
from cassandra import ConsistencyLevel

logger = logging.getLogger(__name__)

_TABLE = "layout_elements"

# CQL to create the table if it does not exist.
_CREATE_TABLE_CQL = f"""
CREATE TABLE IF NOT EXISTS {{keyspace}}.{_TABLE} (
    doc_id      text,
    element_id  text,
    page        int,
    element_type text,
    bbox_x0     double,
    bbox_y0     double,
    bbox_x1     double,
    bbox_y1     double,
    text        text,
    PRIMARY KEY (doc_id, element_id)
);
"""

_INSERT_CQL = f"""
INSERT INTO {{keyspace}}.{_TABLE}
    (doc_id, element_id, page, element_type, bbox_x0, bbox_y0, bbox_x1, bbox_y1, text)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

_SELECT_CQL = f"SELECT * FROM {{keyspace}}.{_TABLE} WHERE doc_id = ?;"
_DELETE_CQL = f"DELETE FROM {{keyspace}}.{_TABLE} WHERE doc_id = ?;"


def _build_session() -> tuple[Cluster, Session, str]:
    """Connect to AstraDB and return (cluster, session, keyspace)."""
    token = os.environ["ASTRA_DB_APPLICATION_TOKEN"]
    endpoint = os.environ["ASTRA_DB_API_ENDPOINT"].rstrip("/")
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace")

    # AstraDB uses the Stargate Cassandra-compatible endpoint on port 29042
    # or the secure-connect bundle.  When only the REST endpoint is supplied
    # we derive the CQL hostname from it.
    cql_host = endpoint.split("://", 1)[-1]  # strip scheme

    auth = PlainTextAuthProvider(username="token", password=token)
    cluster = Cluster(
        contact_points=[cql_host],
        port=29042,
        auth_provider=auth,
        load_balancing_policy=DCAwareRoundRobinPolicy(),
        protocol_version=4,
    )
    session: Session = cluster.connect(keyspace)
    session.default_consistency_level = ConsistencyLevel.LOCAL_QUORUM
    return cluster, session, keyspace


class AstraLayoutStore:
    """Store for Docling bbox/layout elements, backed by AstraDB (IBM DataStax).

    Each element is a row with (doc_id, element_id) as the primary key plus
    structured coordinate and text columns.  No JSON sidecars are written to
    COS.
    """

    def __init__(self) -> None:
        self._cluster, self._session, self._keyspace = _build_session()
        self._ensure_table()
        self._insert_stmt = self._session.prepare(
            _INSERT_CQL.format(keyspace=self._keyspace)
        )
        self._select_stmt = self._session.prepare(
            _SELECT_CQL.format(keyspace=self._keyspace)
        )
        self._delete_stmt = self._session.prepare(
            _DELETE_CQL.format(keyspace=self._keyspace)
        )

    def _ensure_table(self) -> None:
        self._session.execute(
            _CREATE_TABLE_CQL.format(keyspace=self._keyspace)
        )
        logger.debug("Ensured table %s.%s exists", self._keyspace, _TABLE)

    # ── public interface ──────────────────────────────────────────────────────

    def insert_elements(self, doc_id: str, elements: list[dict[str, Any]]) -> None:
        """Persist a list of Docling layout elements for a document.

        Each element dict must have at least:
            element_id   str
            page         int
            element_type str
            bbox         [x0, y0, x1, y1]  (floats)
            text         str  (may be empty)

        Existing rows for the same doc_id are overwritten (upsert semantics).
        """
        for el in elements:
            bbox = el.get("bbox") or [0.0, 0.0, 0.0, 0.0]
            self._session.execute(
                self._insert_stmt,
                (
                    doc_id,
                    str(el["element_id"]),
                    int(el.get("page", 0)),
                    str(el.get("element_type", "")),
                    float(bbox[0]),
                    float(bbox[1]),
                    float(bbox[2]),
                    float(bbox[3]),
                    str(el.get("text", "")),
                ),
            )
        logger.info(
            "Stored %d layout elements for doc_id=%s", len(elements), doc_id
        )

    def get_elements(self, doc_id: str) -> list[dict[str, Any]]:
        """Return all layout elements stored for *doc_id*."""
        rows = self._session.execute(self._select_stmt, (doc_id,))
        return [
            {
                "doc_id": r.doc_id,
                "element_id": r.element_id,
                "page": r.page,
                "element_type": r.element_type,
                "bbox": [r.bbox_x0, r.bbox_y0, r.bbox_x1, r.bbox_y1],
                "text": r.text,
            }
            for r in rows
        ]

    def delete_elements(self, doc_id: str) -> None:
        """Delete all layout elements for *doc_id*."""
        self._session.execute(self._delete_stmt, (doc_id,))
        logger.info("Deleted layout elements for doc_id=%s", doc_id)

    def close(self) -> None:
        self._cluster.shutdown()
