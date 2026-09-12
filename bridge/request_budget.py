"""Per-operation upstream work accounting; cache hits do not spend this budget."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

from .core import BridgeError

_current = ContextVar('github_request_budget', default=None)


class RequestBudget:
    def __init__(self, limit):
        self.limit, self.used = limit, 0
        self.lock = threading.Lock()

    def consume(self):
        with self.lock:
            if self.used >= self.limit:
                raise BridgeError('github_request_budget_exceeded', 429,
                                  request_limit=self.limit, upstream_requests=self.used)
            self.used += 1


def spend_request():
    budget = _current.get()
    if budget is not None:
        budget.consume()


@contextmanager
def request_budget(limit):
    budget = RequestBudget(limit)
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)
