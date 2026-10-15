from autoheal_gateway.circuit import CircuitBreaker, State


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make() -> tuple[CircuitBreaker, Clock]:
    clock = Clock()
    return CircuitBreaker("test", failure_threshold=3, reset_seconds=10, clock=clock), clock


def test_opens_after_consecutive_failures() -> None:
    cb, _ = make()
    for _ in range(2):
        cb.record_failure()
    assert cb.state is State.CLOSED and cb.allow()
    cb.record_failure()
    assert cb.state is State.OPEN and not cb.allow()


def test_success_resets_failure_count() -> None:
    cb, _ = make()
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state is State.CLOSED


def test_half_open_allows_single_trial_then_closes_on_success() -> None:
    cb, clock = make()
    for _ in range(3):
        cb.record_failure()
    clock.now = 10.5
    assert cb.state is State.HALF_OPEN
    assert cb.allow()
    assert not cb.allow()  # only one probe in flight
    cb.record_success()
    assert cb.state is State.CLOSED and cb.allow()


def test_failed_trial_reopens() -> None:
    cb, clock = make()
    for _ in range(3):
        cb.record_failure()
    clock.now = 11
    assert cb.allow()
    cb.record_failure()
    assert cb.state is State.OPEN
    assert cb.retry_after() == 10
