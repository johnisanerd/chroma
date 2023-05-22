from chromadb.db.base import SqlDB, ParameterValue, get_sql
from chromadb.ingest import (
    Producer,
    Consumer,
    encode_vector,
    decode_vector,
    ConsumerCallbackFn,
    RejectedEmbeddingException,
)
from chromadb.types import (
    InsertEmbeddingRecord,
    EmbeddingRecord,
    DeleteEmbeddingRecord,
    EmbeddingDeleteRecord,
    SeqId,
    ScalarEncoding,
)
from chromadb.config import System
from overrides import override
from collections import defaultdict
from typing import Tuple, Optional, Dict, Set, Union
from uuid import UUID
from pypika import Table, functions
import uuid
import json
import logging

logger = logging.getLogger(__name__)


class EmbeddingsQueue(SqlDB, Producer, Consumer):
    """A SQL database that stores embeddings, allowing a traditional RDBMS to be used as
    the primary ingest queue and satisfying the top level Producer/Consumer interfaces.

    Note that this class is only suitable for use cases where the producer and consumer
    are in the same process.

    This is because notifiaction of new embeddings happens solely in-process: this
    implementation does not actively listen to the the database for new records added by
    other processes.
    """

    class Subscription:
        id: UUID
        topic_name: str
        start: int
        end: int
        callback: ConsumerCallbackFn

        def __init__(
            self,
            id: UUID,
            topic_name: str,
            start: int,
            end: int,
            callback: ConsumerCallbackFn,
        ):
            self.id = id
            self.topic_name = topic_name
            self.start = start
            self.end = end
            self.callback = callback

    _subscriptions: Dict[str, Set[Subscription]]

    def __init__(self, system: System):
        self._subscriptions = defaultdict(set)
        super().__init__(system)

    @override
    def create_topic(self, topic_name: str) -> None:
        # Topic creation is implicit for this impl
        pass

    @override
    def delete_topic(self, topic_name: str) -> None:
        t = Table("embeddings_queue")
        q = (
            self.querybuilder()
            .from_(t)
            .where(t.topic == ParameterValue(topic_name))
            .delete()
        )
        with self.tx() as cur:
            sql, params = get_sql(q, self.parameter_format())
            cur.execute(sql, params)

    @override
    def submit_embedding(
        self, topic_name: str, embedding: InsertEmbeddingRecord, sync: bool = False
    ) -> None:
        embedding_bytes = encode_vector(embedding["embedding"], embedding["encoding"])
        metadata = json.dumps(embedding["metadata"]) if embedding["metadata"] else None

        t = Table("embeddings_queue")
        insert = (
            self.querybuilder()
            .into(t)
            .columns(t.topic, t.id, t.is_delete, t.vector, t.encoding, t.metadata)
            .insert(
                ParameterValue(topic_name),
                ParameterValue(embedding["id"]),
                False,
                ParameterValue(embedding_bytes),
                ParameterValue(embedding["encoding"].value),
                ParameterValue(metadata),
            )
        )
        with self.tx() as cur:
            sql, params = get_sql(insert, self.parameter_format())
            sql = f"{sql} RETURNING seq_id"  # Pypika doesn't support RETURNING
            seq_id = int(cur.execute(sql, params).fetchone()[0])
            embedding_record = EmbeddingRecord(
                id=embedding["id"],
                seq_id=seq_id,
                embedding=embedding["embedding"],
                encoding=embedding["encoding"],
                metadata=embedding["metadata"],
            )
            self._notify_all(topic_name, sync, embedding_record)

    @override
    def submit_embedding_delete(
        self,
        topic_name: str,
        delete_embedding: DeleteEmbeddingRecord,
        sync: bool = False,
    ) -> None:
        t = Table("embeddings_queue")
        insert = (
            self.querybuilder()
            .into(t)
            .columns(t.topic, t.id, t.is_delete)
            .insert(
                ParameterValue(topic_name),
                ParameterValue(delete_embedding["delete_id"]),
                True,
            )
        )
        with self.tx() as cur:
            sql, params = get_sql(insert, self.parameter_format())
            sql = f"{sql} RETURNING seq_id"  # Pypika doesn't support RETURNING
            seq_id = int(cur.execute(sql, params).fetchone()[0])
            self._notify_all(
                topic_name,
                sync,
                EmbeddingDeleteRecord(
                    seq_id=seq_id,
                    delete_id=delete_embedding["delete_id"],
                ),
            )

    @override
    def subscribe(
        self,
        topic_name: str,
        consume_fn: ConsumerCallbackFn,
        start: Optional[SeqId] = None,
        end: Optional[SeqId] = None,
        id: Optional[UUID] = None,
    ) -> UUID:
        subscription_id = id or uuid.uuid4()
        start, end = self._validate_range(start, end)

        subscription = self.Subscription(
            subscription_id, topic_name, start, end, consume_fn
        )

        # Backfill first, so if it errors we do not add the subscription
        self._backfill(subscription)
        self._subscriptions[topic_name].add(subscription)

        return subscription_id

    @override
    def unsubscribe(self, subscription_id: UUID) -> None:
        for topic_name, subscriptions in self._subscriptions.items():
            for subscription in subscriptions:
                if subscription.id == subscription_id:
                    subscriptions.remove(subscription)
                    if len(subscriptions) == 0:
                        del self._subscriptions[topic_name]
                    return

    @override
    def min_seqid(self) -> SeqId:
        return -1

    @override
    def max_seqid(self) -> SeqId:
        return 2**63 - 1

    def _backfill(self, subscription: Subscription) -> None:
        """Backfill the given subscription with any currently matching records in the
        DB"""
        t = Table("embeddings_queue")
        q = (
            self.querybuilder()
            .from_(t)
            .where(t.topic == ParameterValue(subscription.topic_name))
            .where(t.seq_id > ParameterValue(subscription.start))
            .where(t.seq_id <= ParameterValue(subscription.end))
            .select(t.seq_id, t.id, t.is_delete, t.vector, t.encoding, t.metadata)
            .orderby(t.seq_id)
        )
        with self.tx() as cur:
            sql, params = get_sql(q, self.parameter_format())
            cur.execute(sql, params)
            rows = cur.fetchall()
            for row in rows:
                if row[2]:
                    self._notify_one(
                        subscription,
                        False,
                        EmbeddingDeleteRecord(seq_id=row[0], delete_id=row[1]),
                    )
                else:
                    encoding = ScalarEncoding(row[4])
                    vector = decode_vector(row[3], encoding)
                    self._notify_one(
                        subscription,
                        False,
                        EmbeddingRecord(
                            seq_id=row[0],
                            id=row[1],
                            embedding=vector,
                            encoding=encoding,
                            metadata=json.loads(row[5]) if row[5] else None,
                        ),
                    )

    def _validate_range(
        self, start: Optional[SeqId], end: Optional[SeqId]
    ) -> Tuple[int, int]:
        """Validate and normalize the start and end SeqIDs for a subscription using this
        impl."""
        start = start or self._next_seq_id()
        end = end or self.max_seqid()
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("SeqIDs must be integers for sql-based EmbeddingsDB")
        if start >= end:
            raise ValueError(f"Invalid SeqID range: {start} to {end}")
        return start, end

    def _next_seq_id(self) -> int:
        """Get the next SeqID for this database."""
        t = Table("embeddings_queue")
        q = self.querybuilder().from_(t).select(functions.Max(t.seq_id))
        with self.tx() as cur:
            cur.execute(q.get_sql())
            return int(cur.fetchone()[0]) + 1

    def _notify_all(
        self,
        topic: str,
        sync: bool,
        embedding: Union[EmbeddingRecord, EmbeddingDeleteRecord],
    ) -> None:
        """Send a notification to each subscriber of the given topic."""
        for sub in self._subscriptions[topic]:
            self._notify_one(sub, sync, embedding)

    def _notify_one(
        self,
        sub: Subscription,
        sync: bool,
        embedding: Union[EmbeddingRecord, EmbeddingDeleteRecord],
    ) -> None:
        """Send a notification to a single subscriber."""
        if embedding["seq_id"] > sub.end:
            self.unsubscribe(sub.id)
            return

        if embedding["seq_id"] <= sub.start:
            return

        if sync:
            sub.callback([embedding])
        else:
            try:
                sub.callback([embedding])
            except RejectedEmbeddingException as e:
                id = embedding.get("id", embedding.get("delete_id"))
                logger.info(
                    f"Consumer {sub.id} to topic {sub.topic_name} rejected"
                    + f"embedding {id}: {e}"
                )