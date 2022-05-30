"""Provides classes for performing a table diff
"""

import time
from operator import methodcaller
from collections import defaultdict
from typing import List, Tuple
import logging
from concurrent.futures import ThreadPoolExecutor

from runtype import dataclass

from .sql import Select, Checksum, Compare, DbPath, DbKey, DbTime, Count, TableName, Time, Min, Max
from .database import Database

logger = logging.getLogger("diff_tables")

RECOMMENDED_CHECKSUM_DURATION = 10

DEFAULT_BISECTION_THRESHOLD = 1024 * 16


def safezip(*args):
    "zip but makes sure all sequences are the same length"
    assert len(set(map(len, args))) == 1
    return zip(*args)


def split_space(start, end, count):
    size = end - start
    return list(range(start, end, (size + 1) // (count + 1)))[1 : count + 1]


@dataclass(frozen=False)
class TableSegment:
    database: Database
    table_path: DbPath
    key_column: str
    update_column: str = None
    extra_columns: Tuple[str, ...] = ()
    start_key: DbKey = None
    end_key: DbKey = None
    min_time: DbTime = None
    max_time: DbTime = None

    def __post_init__(self):
        if not self.update_column and (self.min_time or self.max_time):
            raise ValueError("Error: min_time/max_time feature requires to specify 'update_column'")

    def _make_key_range(self):
        if self.start_key is not None:
            yield Compare("<=", str(self.start_key), self.key_column)
        if self.end_key is not None:
            yield Compare("<", self.key_column, str(self.end_key))

    def _make_update_range(self):
        if self.min_time is not None:
            yield Compare("<=", Time(self.min_time), self.update_column)
        if self.max_time is not None:
            yield Compare("<", self.update_column, Time(self.max_time))

    def _make_select(self, *, table=None, columns=None, where=None, group_by=None, order_by=None):
        if columns is None:
            columns = [self.key_column]
        where = list(self._make_key_range()) + list(self._make_update_range()) + ([] if where is None else [where])
        order_by = None if order_by is None else [order_by]
        return Select(
            table=table or TableName(self.table_path),
            where=where,
            columns=columns,
            group_by=group_by,
            order_by=order_by,
        )

    def get_values(self) -> list:
        "Download all the relevant values of the segment from the database"
        select = self._make_select(columns=self._relevant_columns)
        return self.database.query(select, List[Tuple])

    def choose_checkpoints(self, count: int) -> List[DbKey]:
        "Suggests a bunch of evenly-spaced checkpoints to split by (not including start, end)"
        assert self.is_bounded
        return split_space(self.start_key, self.end_key, count)

    def segment_by_checkpoints(self, checkpoints: List[DbKey]) -> List["TableSegment"]:
        "Split the current TableSegment to a bunch of smaller ones, separate by the given checkpoints"

        if self.start_key and self.end_key:
            assert all(self.start_key <= c < self.end_key for c in checkpoints)
        checkpoints.sort()

        # Calculate sub-segments
        positions = [self.start_key] + checkpoints + [self.end_key]
        ranges = list(zip(positions[:-1], positions[1:]))

        # Create table segments
        tables = [self.new(start_key=s, end_key=e) for s, e in ranges]

        return tables

    def new(self, **kwargs) -> "TableSegment":
        """Using new() creates a copy of the instance using 'replace()', and makes sure the cache is reset"""
        return self.replace(**kwargs)

    @property
    def _relevant_columns(self) -> List[str]:
        extras = set(self.extra_columns)
        if self.update_column:
            extras.add(self.update_column)

        return [self.key_column] + list(sorted(extras))

    def count(self) -> Tuple[int, int]:
        return self.database.query(self._make_select(columns=[Count()]), int)

    def count_and_checksum(self) -> Tuple[int, int]:
        start = time.time()
        count, checksum = self.database.query(
            self._make_select(columns=[Count(), Checksum(self._relevant_columns)]), tuple
        )
        duration = time.time() - start
        if duration > RECOMMENDED_CHECKSUM_DURATION:
            logger.warn(
                f"Checksum is taking longer than expected ({duration:.2f}s). "
                "We recommend increasing the bisection-factor."
            )

        # TODO Handle None TODO
        return count or 0, checksum

    def query_key_range(self) -> Tuple[int, int]:
        """Query database for minimum and maximum key. This is used for setting the initial bounds."""
        select = self._make_select(columns=[Min(self.key_column), Max(self.key_column)])
        min_key, max_key = self.database.query(select, tuple)

        if min_key is None or max_key is None:
            raise ValueError("Table appears to be empty")

        return min_key, max_key

    @property
    def is_bounded(self):
        return self.start_key is not None and self.end_key is not None


def diff_sets(a: set, b: set) -> iter:
    s1 = set(a)
    s2 = set(b)
    d = defaultdict(list)

    # The first item is always the key (see TableDiffer._relevant_columns)
    for i in s1 - s2:
        d[i[0]].append(("+", i))
    for i in s2 - s1:
        d[i[0]].append(("-", i))

    for k, v in sorted(d.items(), key=lambda i: i[0]):
        yield from v


DiffResult = iter  # Iterator[Tuple[Literal["+", "-"], tuple]]


@dataclass
class TableDiffer:
    """Finds the diff between two SQL tables

    The algorithm uses hashing to quickly check if the tables are different, and then applies a
    bisection search recursively to find the differences efficiently.

    Works best for comparing tables that are mostly the name, with minor discrepencies.
    """

    bisection_factor: int = 32  # Into how many segments to bisect per iteration
    bisection_threshold: int = (
        DEFAULT_BISECTION_THRESHOLD  # When should we stop bisecting and compare locally (in row count)
    )
    debug: bool = False
    threaded: bool = True

    stats: dict = {}

    # Maximum size of each threadpool. None = auto. Only relevant when threaded is True.
    # There may be many pools, so number of actual threads can be a lot higher.
    max_threadpool_size: int = None

    def diff_tables(self, table1: TableSegment, table2: TableSegment) -> DiffResult:
        """Diff the given tables.

        Returned value is an iterator that yield pair-tuples, representing the diff. Items can be either
            ('+', columns) for items in table1 but not in table2
            ('-', columns) for items in table2 but not in table1
            Where `columns` is a tuple of values for the involved columns, i.e. (id, ...extra)
        """
        if self.bisection_factor >= self.bisection_threshold:
            raise ValueError("Incorrect param values (bisection factor must be lower than threshold)")
        if self.bisection_factor < 2:
            raise ValueError("Must have at least two segments per iteration (i.e. bisection_factor >= 2)")

        logger.info(
            f"Diffing tables | segments: {self.bisection_factor}, bisection threshold: {self.bisection_threshold}."
        )

        key_ranges = self._threaded_call("query_key_range", [table1, table2])
        mins, maxs = zip(*key_ranges)

        # We add 1 because our ranges are exclusive of the end (like in Python)
        start_key = min(mins)
        end_key = max(maxs) + 1

        table1 = table1.new(start_key=start_key, end_key=end_key)
        table2 = table2.new(start_key=start_key, end_key=end_key)

        return self._bisect_and_diff_tables(table1, table2)

    def _bisect_and_diff_tables(self, table1, table2, level=0, max_rows=None):
        assert table1.is_bounded and table2.is_bounded

        if max_rows is None:
            # We can be sure that row_count <= max_rows
            max_rows = table1.end_key - table1.start_key

        # If count is below the threshold, just download and compare the columns locally
        # This saves time, as bisection speed is limited by ping and query performance.
        if max_rows < self.bisection_threshold:
            rows1, rows2 = self._threaded_call("get_values", [table1, table2])
            diff = list(diff_sets(rows1, rows2))
            logger.info(". " * level + f"Diff found {len(diff)} different rows.")
            yield from diff
            return

        # Choose evenly spaced checkpoints (according to start_key and end_key)
        checkpoints = table1.choose_checkpoints(self.bisection_factor - 1)

        # Create new instances of TableSegment between each checkpoint
        segmented1 = table1.segment_by_checkpoints(checkpoints)
        segmented2 = table2.segment_by_checkpoints(checkpoints)

        # Recursively compare each pair of corresponding segments between table1 and table2
        diff_iters = [
            self._diff_tables(t1, t2, level + 1, i + 1, len(segmented1))
            for i, (t1, t2) in enumerate(safezip(segmented1, segmented2))
        ]

        for res in self._thread_map(list, diff_iters):
            yield from res

    def _diff_tables(self, table1, table2, level=0, segment_index=None, segment_count=None):
        logger.info(
            ". " * level
            + f"Diffing segment {segment_index}/{segment_count}, "
            f"key-range: {table1.start_key}..{table2.end_key}, "
            f"size: {table2.end_key-table1.start_key}"
        )

        (count1, checksum1), (count2, checksum2) = self._threaded_call("count_and_checksum", [table1, table2])

        if count1 == 0 and count2 == 0:
            logger.warn(
                "Uneven distribution of keys detected. (big gaps in the key column). "
                "For better performance, we recommend to increase the bisection-threshold."
            )
            assert checksum1 is None and checksum2 is None
            return

        if level == 1:
            self.stats["table1_count"] = self.stats.get("table1_count", 0) + count1

        if checksum1 != checksum2:
            yield from self._bisect_and_diff_tables(table1, table2, level=level, max_rows=max(count1, count2))

    def _thread_map(self, func, iter):
        if not self.threaded:
            return map(func, iter)

        task_pool = ThreadPoolExecutor(max_workers=self.max_threadpool_size)
        return task_pool.map(func, iter)

    def _threaded_call(self, func, iter):
        return list(self._thread_map(methodcaller(func), iter))
