"""In-process counters for the health endpoint.

The engine is a single node (internal tool), so process-local counters are the
honest metric source; the DB is the authority for anything that must survive a
restart (queue depth is recomputed on demand, orphan counts from recovery).
"""


class Metrics:
    def __init__(self) -> None:
        self.queue_depth = 0          # pending work items (set each worker poll)
        self.orphaned_items = 0       # stale claimed items found at last recovery
        self.active_dispatches = 0    # dispatch tasks currently running

    def snapshot(self) -> dict:
        return {
            "queue_depth": self.queue_depth,
            "orphaned_items": self.orphaned_items,
            "active_dispatches": self.active_dispatches,
        }


METRICS = Metrics()
