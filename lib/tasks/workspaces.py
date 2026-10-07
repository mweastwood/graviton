"""
Workspace cleanup, garbage collection, and bounded pruned task tracking.
"""

import collections
import logging
import subprocess
import time
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set, Union

from lib.supervisor import clean_workspace_dir

logger = logging.getLogger("graviton.tasks")

def prune_abandoned_workspaces(
    base_dir: Union[Path, str] = Path("/tmp/graviton-workspaces"),
    max_age_seconds: int = 86400,
) -> int:
    """
    Garbage collect abandoned /tmp/graviton-workspaces/run-* directories and stale cache entries
    older than max_age_seconds (default 24 hours) whose corresponding Docker containers are not running.
    Returns the number of cleaned up workspace directories.
    """
    base = Path(base_dir)
    if not base.exists() or not base.is_dir():
        return 0

    now = time.time()
    targets: List[Path] = []
    try:
        for entry in base.iterdir():
            if entry.is_dir():
                if entry.name.startswith("run-"):
                    targets.append(entry)
                elif entry.name == "cache":
                    for cache_entry in entry.iterdir():
                        if cache_entry.is_dir():
                            targets.append(cache_entry)
    except Exception as e:
        logger.warning(f"Error scanning workspace directory {base} for GC: {e}")
        return 0

    if not targets:
        return 0

    aged_targets: List[Path] = []
    has_aged_run = False
    for target in targets:
        try:
            mtime = target.stat().st_mtime
            if (now - mtime) >= max_age_seconds:
                aged_targets.append(target)
                if target.name.startswith("run-"):
                    has_aged_run = True
        except Exception:
            pass

    if not aged_targets:
        return 0

    running_containers = set()
    if has_aged_run:
        try:
            subproc_run = subprocess.run
            import sys
            mod = sys.modules.get("lib.tasks")
            if mod and hasattr(mod, "subprocess") and hasattr(mod.subprocess, "run"):
                subproc_run = mod.subprocess.run
            res = subproc_run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0:
                running_containers = {c.strip() for c in res.stdout.splitlines() if c.strip()}
        except Exception:
            pass

    pruned_count = 0
    for target in aged_targets:
        try:
            if target.name.startswith("run-"):
                run_id = target.name[len("run-"):]
                container_name = f"graviton-agent-run-{run_id}"
                stream_container_name = f"graviton-stream-run-{run_id}"
                if container_name in running_containers or stream_container_name in running_containers:
                    continue
            if clean_workspace_dir(target):
                pruned_count += 1
                logger.info(f"Garbage collected abandoned workspace directory: {target}")
        except Exception as err:
            logger.debug(f"Could not prune workspace directory {target}: {err}")

    return pruned_count


class _PrunedTaskIds:
    """Bounded collection tracking pruned task IDs with O(1) lookups and FIFO eviction."""

    def __init__(
        self,
        iterable: Optional[Union[Iterable[str], int]] = None,
        maxlen: Optional[int] = 10000,
    ):
        # Backwards compatibility when maxlen is passed positionally as first argument
        if isinstance(iterable, int) and not isinstance(iterable, bool):
            if maxlen == 10000 or maxlen is None or maxlen == iterable:
                self._maxlen = iterable
            else:
                self._maxlen = maxlen
            iterable = None
        else:
            self._maxlen = maxlen
        self._items: collections.OrderedDict[str, None] = collections.OrderedDict()
        if iterable is not None:
            for item in iterable:
                self.add(item)

    @property
    def maxlen(self) -> Optional[int]:
        return self._maxlen

    @maxlen.setter
    def maxlen(self, value: Optional[int]) -> None:
        self._maxlen = value
        if self._maxlen is not None:
            if self._maxlen <= 0:
                self._items.clear()
            else:
                while len(self._items) > self._maxlen and self._items:
                    self._items.popitem(last=False)

    def add(self, item: str) -> None:
        """Add an item to the collection, moving it to the most recent position if present."""
        if self.maxlen is not None and self.maxlen <= 0:
            self._items.clear()
            return

        if item in self._items:
            self._items.move_to_end(item)
        else:
            self._items[item] = None

        if self.maxlen is not None:
            while len(self._items) > self.maxlen and self._items:
                self._items.popitem(last=False)

    def discard(self, item: object) -> None:
        """Remove an item from the collection if it is present."""
        try:
            self._items.pop(item, None)
        except TypeError:
            pass

    def remove(self, item: object) -> None:
        """Remove an item from the collection. Raises KeyError if not present."""
        try:
            del self._items[item]
        except (KeyError, TypeError):
            raise KeyError(item) from None

    def __contains__(self, item: object) -> bool:
        try:
            return item in self._items
        except TypeError:
            return False

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __reversed__(self) -> Iterator[str]:
        return reversed(self._items)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({list(self._items)}, maxlen={self.maxlen})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _PrunedTaskIds):
            return NotImplemented
        return self.maxlen == other.maxlen and self._items == other._items

    def copy(self) -> "_PrunedTaskIds":
        """Return a shallow copy of the collection with an independent underlying items dictionary."""
        new_instance = self.__class__(maxlen=self.maxlen)
        new_instance._items = self._items.copy()
        return new_instance

    def __copy__(self) -> "_PrunedTaskIds":
        return self.copy()

    def clear(self) -> None:
        """Remove all items from the collection."""
        self._items.clear()



