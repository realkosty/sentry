import logging
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence, Set

import google.api_core.exceptions
from django.conf import settings
from google.cloud import spanner

from sentry.sentry_metrics.configuration import IndexerStorage, UseCaseKey, get_ingest_config
from sentry.sentry_metrics.indexer.base import (
    FetchType,
    KeyCollection,
    KeyResult,
    KeyResults,
    StringIndexer,
)
from sentry.sentry_metrics.indexer.cache import CachingIndexer, StringIndexerCache
from sentry.sentry_metrics.indexer.cloudspanner.cloudspanner_model import (
    DATABASE_PARAMETERS,
    SpannerIndexerModel,
    get_column_names,
)
from sentry.sentry_metrics.indexer.id_generator import get_id, reverse_bits
from sentry.sentry_metrics.indexer.ratelimiters import writes_limiter_factory
from sentry.sentry_metrics.indexer.strings import StaticStringIndexer
from sentry.utils import metrics
from sentry.utils.codecs import Codec

logger = logging.getLogger(__name__)

EncodedId = int
DecodedId = int

# TODO: This is a temporary hack to get around the fact that majority
# of the graphs we have use postgres. This should be changed to
# "cloudspanner" once we move to production.
_CLOUDSPANNER_SUFFIX = "postgres"
_INDEXER_DB_METRIC = f"sentry_metrics.indexer.{_CLOUDSPANNER_SUFFIX}"
_INDEXER_DB_INSERT_METRIC = f"sentry_metrics.indexer.{_CLOUDSPANNER_SUFFIX}.insert"
_INDEXER_DB_RW_TRANSACTION_FAILED_METRIC = (
    f"sentry_metrics.indexer.{_CLOUDSPANNER_SUFFIX}.rw_transaction_failed"
)
_DEFAULT_RETENTION_DAYS = 90
_PARTITION_KEY = "cs"
_MAX_CONSECUTIVE_ID_GENERATION_FAILURES = 5

indexer_cache = StringIndexerCache(
    **settings.SENTRY_STRING_INDEXER_CACHE_OPTIONS, partition_key=_PARTITION_KEY
)


class CloudSpannerRowAlreadyExists(Exception):
    """
    Exception raised when we insert a row that already exists.
    """

    pass


class IDGenerationError(Exception):
    """
    Exception raised when the same id is generated consecutively.
    """

    pass


class IdCodec(Codec[DecodedId, EncodedId]):
    """
    Encodes 63 bit IDs generated by the id_generator so that they are well distributed for CloudSpanner.

    Given an ID, this codec does the following:
    - reverses the bits and shifts to the left by one
    - Subtract 2^63 so that that the unsigned 64 bit integer now fits in a signed 64 bit field
    """

    def encode(self, value: DecodedId) -> EncodedId:
        return reverse_bits(value, 64) - 2**63

    def decode(self, value: EncodedId) -> DecodedId:
        return reverse_bits(value + 2**63, 64)


class RawCloudSpannerIndexer(StringIndexer):
    """
    Provides integer IDs for metric names, tag keys and tag values
    and the corresponding reverse lookup.
    """

    def __init__(
        self,
        instance_id: str,
        database_id: str,
    ) -> None:
        self.instance_id = instance_id
        self.database_id = database_id
        spanner_client = spanner.Client()
        self.instance = spanner_client.instance(self.instance_id)
        self.database = self.instance.database(self.database_id)
        self.__codec = IdCodec()

    @staticmethod
    def _get_table_name(use_case_id: UseCaseKey) -> str:
        return DATABASE_PARAMETERS[use_case_id].get("table_name", "")

    @staticmethod
    def _get_unique_org_string_index_name(use_case_id: UseCaseKey) -> str:
        return DATABASE_PARAMETERS[use_case_id].get("unique_organization_string_index_name", "")

    def validate(self) -> None:
        """
        Run a simple query to ensure the database is accessible.
        """
        with self.database.snapshot() as snapshot:
            try:
                snapshot.execute_sql("SELECT 1")
            except ValueError:
                # TODO: What is the correct way to handle connection errors?
                pass

    def _get_db_records(
        self, use_case_id: UseCaseKey, db_keys: KeyCollection
    ) -> Sequence[KeyResult]:
        spanner_keyset = []
        for organization_id, string in db_keys.as_tuples():
            spanner_keyset.append([organization_id, string])

        with self.database.snapshot() as snapshot:
            results = snapshot.read(
                table=self._get_table_name(use_case_id),
                columns=["organization_id", "string", "id"],
                keyset=spanner.KeySet(keys=spanner_keyset),
                index=self._get_unique_org_string_index_name(use_case_id),
            )

        results_list = list(results)
        return [
            KeyResult(org_id=row[0], string=row[1], id=self.__codec.decode(row[2]))
            for row in results_list
        ]

    def _create_db_records(self, db_keys: KeyCollection) -> Sequence[SpannerIndexerModel]:
        """
        This method is used to create the set of database records that will be
        inserted into the database.
        The database records are created from the provided dk_keys collection.
        The ID's for the database records are generated by the id_generator.
        We don't want same ID's to be present in 1 batch of database records.
        If the same ID is generated consecutively for
        _MAX_CONSECUTIVE_ID_GENERATION_FAILURES, we raise an exception.
        """
        rows_to_insert = []
        now = datetime.now()
        already_assigned_ids = set()
        for organization_id, string in db_keys.as_tuples():
            failed_id_generation_count = 0
            new_id = get_id()
            while new_id in already_assigned_ids:
                if failed_id_generation_count > _MAX_CONSECUTIVE_ID_GENERATION_FAILURES:
                    raise IDGenerationError
                failed_id_generation_count += 1
                new_id = get_id()

            model = SpannerIndexerModel(
                id=self.__codec.encode(new_id),
                decoded_id=new_id,
                string=string,
                organization_id=organization_id,
                date_added=now,
                last_seen=now,
                retention_days=_DEFAULT_RETENTION_DAYS,
            )
            already_assigned_ids.add(new_id)
            rows_to_insert.append(model)

        return rows_to_insert

    def _insert_db_records(
        self,
        use_case_id: UseCaseKey,
        rows_to_insert: Sequence[SpannerIndexerModel],
        key_results: KeyResults,
    ) -> None:
        """
        Insert a bunch of db_keys records into the database. When there is a
        success of the insert, we update the key_results with the records
        that were inserted. This is different from the postgres implementation
        because postgres implementation uses ignore_conflicts=True. With spanner,
        there is no such option. Once a write is successful, we can assume that
        there were no conflicts and can avoid performing the additional lookup
        to check whether DB records match with what we tried to insert.
        """
        try:
            self._insert_collisions_not_handled(use_case_id, rows_to_insert, key_results)
        except CloudSpannerRowAlreadyExists:
            self._insert_collisions_handled(use_case_id, rows_to_insert, key_results)

    def _insert_collisions_not_handled(
        self,
        use_case_id: UseCaseKey,
        rows_to_insert: Sequence[SpannerIndexerModel],
        key_results: KeyResults,
    ) -> None:
        """
        Insert a batch of records in a transaction. This is the preferred
        way of inserting records as it will reduce the number of operations
        which need to be performed in a transaction. If the insert succeeds,
        we update the key_results with the records that were inserted.

        The transaction could fail if there are collisions with existing
        records. In such a case, none of the records which we are trying to
        batch insert will be inserted. We raise a
        CloudSpannerRowAlreadyExists exception.
        """

        def insert_uow(transaction: Any, rows: Sequence[SpannerIndexerModel]) -> None:
            transaction.insert(
                table=self._get_table_name(use_case_id), columns=get_column_names(), values=rows
            )

        try:
            self.database.run_in_transaction(insert_uow, rows_to_insert)
        except (
            google.api_core.exceptions.AlreadyExists,
            google.api_core.exceptions.InvalidArgument,
        ):
            raise CloudSpannerRowAlreadyExists
        else:
            metrics.incr(
                _INDEXER_DB_INSERT_METRIC,
                tags={"batch": "true"},
            )
            key_results.add_key_results(
                [
                    KeyResult(
                        org_id=row.organization_id,
                        string=row.string,
                        id=row.decoded_id,
                    )
                    for row in rows_to_insert
                ],
                fetch_type=FetchType.FIRST_SEEN,
            )

    def _insert_collisions_handled(
        self,
        use_case_id: UseCaseKey,
        rows_to_insert: Sequence[SpannerIndexerModel],
        key_results: KeyResults,
    ) -> None:
        """
        Insert the records in a transaction while handling collisions that
        might happen during an INSERT. Collisions can happen  when multiple
        consumers try to write records with same organization_id and string.

        The basic logic is to retry the below in a loop until the transaction
        succeeds.
        The logic within a transaction is as follows:
        1. Get records from the database for the organization_id and string
        which are present in the rows.
        2. Get records from the database for the id which are present in the rows.
        3. Create a new list of records to be inserted which excludes the
        records which are present in the above two lists.
        4. Insert the new list of records in a transaction.

        Once the transaction succeeds, we update the key_results by
        performing a read of the organization_id and string which were
        supposed to be originally inserted.
        """

        def insert_rw_transaction_uow(
            transaction: Any, rows: Sequence[SpannerIndexerModel]
        ) -> Sequence[SpannerIndexerModel]:
            existing_org_string_results = transaction.read(
                table=self._get_table_name(use_case_id),
                columns=["organization_id", "string", "id"],
                keyset=spanner.KeySet(keys=[[row.organization_id, row.string] for row in rows]),
                index=self._get_unique_org_string_index_name(use_case_id),
            )

            existing_id_results = transaction.read(
                table=self._get_table_name(use_case_id),
                columns=["organization_id", "string", "id"],
                keyset=spanner.KeySet(keys=[[row.id] for row in rows]),
            )

            existing_records = list(existing_org_string_results) + list(existing_id_results)
            existing_org_string_set = {(row[0], row[1]) for row in existing_records}
            existing_id_set = {row[2] for row in existing_records}

            missing_records = []
            for row in rows:
                if row.id in existing_id_set:
                    new_id = get_id()
                    missing_records.append(
                        SpannerIndexerModel(
                            id=self.__codec.encode(new_id),
                            decoded_id=new_id,
                            string=row.string,
                            organization_id=row.organization_id,
                            date_added=row.date_added,
                            last_seen=row.last_seen,
                            retention_days=row.retention_days,
                        )
                    )
                elif (row.organization_id, row.string) not in existing_org_string_set:
                    missing_records.append(row)

            if missing_records:
                transaction.insert(
                    self._get_table_name(use_case_id),
                    columns=get_column_names(),
                    values=missing_records,
                )

            return missing_records

        metrics.incr(
            _INDEXER_DB_INSERT_METRIC,
            tags={"batch": "false"},
        )
        transaction_succeeded = False
        while not transaction_succeeded:
            try:
                rows_inserted = self.database.run_in_transaction(
                    insert_rw_transaction_uow, rows_to_insert
                )
                transaction_succeeded = True
            except google.api_core.exceptions.AlreadyExists:
                metrics.incr(_INDEXER_DB_RW_TRANSACTION_FAILED_METRIC)
            else:
                key_results.add_key_results(
                    [
                        KeyResult(
                            org_id=row.organization_id,
                            string=row.string,
                            id=self.__codec.decode(row.id),
                        )
                        for row in rows_inserted
                    ],
                    fetch_type=FetchType.DB_READ,
                )

    def bulk_record(
        self, use_case_id: UseCaseKey, org_strings: Mapping[int, Set[str]]
    ) -> KeyResults:
        db_read_keys = KeyCollection(org_strings)

        db_read_key_results = KeyResults()
        db_read_key_results.add_key_results(
            self._get_db_records(use_case_id, db_read_keys),
            FetchType.DB_READ,
        )
        db_write_keys = db_read_key_results.get_unmapped_keys(db_read_keys)

        metrics.incr(
            _INDEXER_DB_METRIC,
            tags={"db_hit": "true"},
            amount=(db_read_keys.size - db_write_keys.size),
        )
        metrics.incr(
            _INDEXER_DB_METRIC,
            tags={"db_hit": "false"},
            amount=db_write_keys.size,
        )

        if db_write_keys.size == 0:
            return db_read_key_results

        config = get_ingest_config(use_case_id, IndexerStorage.CLOUDSPANNER)
        writes_limiter = writes_limiter_factory.get_ratelimiter(config)

        with writes_limiter.check_write_limits(use_case_id, db_write_keys) as writes_limiter_state:
            # After the DB has successfully committed writes, we exit this
            # context manager and consume quotas. If the DB crashes we
            # shouldn't consume quota.
            filtered_db_write_keys = writes_limiter_state.accepted_keys
            del db_write_keys

            rate_limited_key_results = KeyResults()
            for dropped_string in writes_limiter_state.dropped_strings:
                rate_limited_key_results.add_key_result(
                    dropped_string.key_result,
                    fetch_type=dropped_string.fetch_type,
                    fetch_type_ext=dropped_string.fetch_type_ext,
                )

            if filtered_db_write_keys.size == 0:
                return db_read_key_results.merge(rate_limited_key_results)

            new_records = self._create_db_records(filtered_db_write_keys)
            db_write_key_results = KeyResults()
            with metrics.timer("sentry_metrics.indexer.pg_bulk_create"):
                self._insert_db_records(use_case_id, new_records, db_write_key_results)

        return db_read_key_results.merge(db_write_key_results).merge(rate_limited_key_results)

    def record(self, use_case_id: UseCaseKey, org_id: int, string: str) -> Optional[int]:
        """Store a string and return the integer ID generated for it"""
        result = self.bulk_record(use_case_id=use_case_id, org_strings={org_id: {string}})
        return result[org_id][string]

    def resolve(self, use_case_id: UseCaseKey, org_id: int, string: str) -> Optional[int]:
        """Resolve a string to an integer ID"""
        with self.database.snapshot() as snapshot:
            results = snapshot.read(
                table=self._get_table_name(use_case_id),
                columns=["id"],
                keyset=spanner.KeySet(keys=[[org_id, string]]),
                index=self._get_unique_org_string_index_name(use_case_id),
            )

        results_list = list(results)
        if len(results_list) == 0:
            return None
        else:
            return int(self.__codec.decode(results_list[0][0]))

    def reverse_resolve(self, use_case_id: UseCaseKey, org_id: int, id: int) -> Optional[str]:
        """Resolve an integer ID to a string"""
        with self.database.snapshot() as snapshot:
            results = snapshot.read(
                table=self._get_table_name(use_case_id),
                columns=["string"],
                keyset=spanner.KeySet(keys=[[id]]),
            )

        results_list = list(results)
        if len(results_list) == 0:
            return None
        else:
            return str(results_list[0][0])


class CloudSpannerIndexer(StaticStringIndexer):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(CachingIndexer(indexer_cache, RawCloudSpannerIndexer(**kwargs)))
