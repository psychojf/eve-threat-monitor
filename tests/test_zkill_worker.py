# -*- coding: utf-8 -*-
# Bounded, cancellable zKill lookup worker tests.
import threading


def test_lookup_pool_never_exceeds_two_active_workers():
    from tm.zkill_worker import LookupPool

    lock = threading.Lock()
    release = threading.Event()
    two_active = threading.Event()
    active = 0
    maximum = 0

    def fetch(name, on_ready, on_error):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_active.set()
        assert release.wait(2)
        with lock:
            active -= 1
        on_ready({"name": name})

    pool = LookupPool(fetcher=fetch, max_workers=2, max_pending=32)
    jobs = [pool.submit(str(index), lambda value: None, lambda error: None)
            for index in range(25)]
    assert two_active.wait(1)
    assert maximum == 2
    for job in jobs:
        job.cancel()
    release.set()
    pool.shutdown()


def test_cancelled_pending_job_never_fetches_or_calls_back():
    from tm.zkill_worker import LookupPool

    entered = threading.Event()
    release = threading.Event()
    fetched = []
    callbacks = []

    def fetch(name, on_ready, on_error):
        fetched.append(name)
        if name == "first":
            entered.set()
            assert release.wait(2)
        on_ready(name)

    pool = LookupPool(fetcher=fetch, max_workers=1, max_pending=4)
    pool.submit("first", callbacks.append, callbacks.append)
    assert entered.wait(1)
    queued = pool.submit("second", callbacks.append, callbacks.append)
    queued.cancel()
    release.set()
    pool.shutdown()

    assert fetched == ["first"]
    assert callbacks == ["first"]


def test_queue_overflow_cancels_oldest_pending_job():
    from tm.zkill_worker import LookupPool

    entered = threading.Event()
    release = threading.Event()

    def fetch(name, on_ready, on_error):
        if name == "active":
            entered.set()
            assert release.wait(2)
        on_ready(name)

    pool = LookupPool(fetcher=fetch, max_workers=1, max_pending=2)
    pool.submit("active", lambda value: None, lambda error: None)
    assert entered.wait(1)
    oldest = pool.submit("oldest", lambda value: None, lambda error: None)
    pool.submit("newer", lambda value: None, lambda error: None)
    pool.submit("newest", lambda value: None, lambda error: None)

    assert oldest.cancelled
    release.set()
    pool.shutdown()


def test_callback_exception_does_not_kill_worker_or_block_next_job():
    from tm.zkill_worker import LookupPool

    second_done = threading.Event()

    def fetch(name, on_ready, on_error):
        on_ready(name)

    def broken_callback(value):
        raise RuntimeError("deleted Qt receiver")

    pool = LookupPool(fetcher=fetch, max_workers=1, max_pending=4)
    pool.submit("first", broken_callback, broken_callback)
    pool.submit("second", lambda value: second_done.set(), broken_callback)

    assert second_done.wait(1)
    pool.shutdown()
