import contextvars
import threading
import queue as stdlib_queue
import time
import weakref

import pytest
from sniffio import current_async_library_cvar
from trio._core import TrioToken, current_trio_token

from .. import _core
from .. import Event, CapacityLimiter, sleep
from ..testing import wait_all_tasks_blocked
from .._core.tests.tutil import buggy_pypy_asyncgens
from .._threads import (
    to_thread_run_sync,
    current_default_thread_limiter,
    from_thread_run,
    from_thread_run_sync,
)

from .._core.tests.test_ki import ki_self


async def test_do_in_trio_thread():
    trio_thread = threading.current_thread()

    async def check_case(do_in_trio_thread, fn, expected, trio_token=None):
        record = []

        def threadfn():
            try:
                record.append(("start", threading.current_thread()))
                x = do_in_trio_thread(fn, record, trio_token=trio_token)
                record.append(("got", x))
            except BaseException as exc:
                print(exc)
                record.append(("error", type(exc)))

        child_thread = threading.Thread(target=threadfn, daemon=True)
        child_thread.start()
        while child_thread.is_alive():
            print("yawn")
            await sleep(0.01)
        assert record == [("start", child_thread), ("f", trio_thread), expected]

    token = _core.current_trio_token()

    def f(record):
        assert not _core.currently_ki_protected()
        record.append(("f", threading.current_thread()))
        return 2

    await check_case(from_thread_run_sync, f, ("got", 2), trio_token=token)

    def f(record):
        assert not _core.currently_ki_protected()
        record.append(("f", threading.current_thread()))
        raise ValueError

    await check_case(from_thread_run_sync, f, ("error", ValueError), trio_token=token)

    async def f(record):
        assert not _core.currently_ki_protected()
        await _core.checkpoint()
        record.append(("f", threading.current_thread()))
        return 3

    await check_case(from_thread_run, f, ("got", 3), trio_token=token)

    async def f(record):
        assert not _core.currently_ki_protected()
        await _core.checkpoint()
        record.append(("f", threading.current_thread()))
        raise KeyError

    await check_case(from_thread_run, f, ("error", KeyError), trio_token=token)


async def test_do_in_trio_thread_from_trio_thread():
    with pytest.raises(RuntimeError):
        from_thread_run_sync(lambda: None)  # pragma: no branch

    async def foo():  # pragma: no cover
        pass

    with pytest.raises(RuntimeError):
        from_thread_run(foo)


def test_run_in_trio_thread_ki():
    # if we get a control-C during a run_in_trio_thread, then it propagates
    # back to the caller (slick!)
    record = set()

    async def check_run_in_trio_thread():
        token = _core.current_trio_token()

        def trio_thread_fn():
            print("in Trio thread")
            assert not _core.currently_ki_protected()
            print("ki_self")
            try:
                ki_self()
            finally:
                import sys

                print("finally", sys.exc_info())

        async def trio_thread_afn():
            trio_thread_fn()

        def external_thread_fn():
            try:
                print("running")
                from_thread_run_sync(trio_thread_fn, trio_token=token)
            except KeyboardInterrupt:
                print("ok1")
                record.add("ok1")
            try:
                from_thread_run(trio_thread_afn, trio_token=token)
            except KeyboardInterrupt:
                print("ok2")
                record.add("ok2")

        thread = threading.Thread(target=external_thread_fn)
        thread.start()
        print("waiting")
        while thread.is_alive():
            await sleep(0.01)
        print("waited, joining")
        thread.join()
        print("done")

    _core.run(check_run_in_trio_thread)
    assert record == {"ok1", "ok2"}


def test_await_in_trio_thread_while_main_exits():
    record = []
    ev = Event()

    async def trio_fn():
        record.append("sleeping")
        ev.set()
        await _core.wait_task_rescheduled(lambda _: _core.Abort.SUCCEEDED)

    def thread_fn(token):
        try:
            from_thread_run(trio_fn, trio_token=token)
        except _core.Cancelled:
            record.append("cancelled")

    async def main():
        token = _core.current_trio_token()
        thread = threading.Thread(target=thread_fn, args=(token,))
        thread.start()
        await ev.wait()
        assert record == ["sleeping"]
        return thread

    thread = _core.run(main)
    thread.join()
    assert record == ["sleeping", "cancelled"]


async def test_run_in_worker_thread():
    trio_thread = threading.current_thread()

    def f(x):
        return (x, threading.current_thread())

    x, child_thread = await to_thread_run_sync(f, 1)
    assert x == 1
    assert child_thread != trio_thread

    def g():
        raise ValueError(threading.current_thread())

    with pytest.raises(ValueError) as excinfo:
        await to_thread_run_sync(g)
    print(excinfo.value.args)
    assert excinfo.value.args[0] != trio_thread


async def test_run_in_worker_thread_cancellation():
    register = [None]

    def f(q):
        # Make the thread block for a controlled amount of time
        register[0] = "blocking"
        q.get()
        register[0] = "finished"

    async def child(q, cancellable):
        record.append("start")
        try:
            return await to_thread_run_sync(f, q, cancellable=cancellable)
        finally:
            record.append("exit")

    record = []
    q = stdlib_queue.Queue()
    async with _core.open_nursery() as nursery:
        nursery.start_soon(child, q, True)
        # Give it a chance to get started. (This is important because
        # to_thread_run_sync does a checkpoint_if_cancelled before
        # blocking on the thread, and we don't want to trigger this.)
        await wait_all_tasks_blocked()
        assert record == ["start"]
        # Then cancel it.
        nursery.cancel_scope.cancel()
    # The task exited, but the thread didn't:
    assert register[0] != "finished"
    # Put the thread out of its misery:
    q.put(None)
    while register[0] != "finished":
        time.sleep(0.01)

    # This one can't be cancelled
    record = []
    register[0] = None
    async with _core.open_nursery() as nursery:
        nursery.start_soon(child, q, False)
        await wait_all_tasks_blocked()
        nursery.cancel_scope.cancel()
        with _core.CancelScope(shield=True):
            for _ in range(10):
                await _core.checkpoint()
        # It's still running
        assert record == ["start"]
        q.put(None)
        # Now it exits

    # But if we cancel *before* it enters, the entry is itself a cancellation
    # point
    with _core.CancelScope() as scope:
        scope.cancel()
        await child(q, False)
    assert scope.cancelled_caught


# Make sure that if trio.run exits, and then the thread finishes, then that's
# handled gracefully. (Requires that the thread result machinery be prepared
# for call_soon to raise RunFinishedError.)
def test_run_in_worker_thread_abandoned(capfd, monkeypatch):
    monkeypatch.setattr(_core._thread_cache, "IDLE_TIMEOUT", 0.01)

    q1 = stdlib_queue.Queue()
    q2 = stdlib_queue.Queue()

    def thread_fn():
        q1.get()
        q2.put(threading.current_thread())

    async def main():
        async def child():
            await to_thread_run_sync(thread_fn, cancellable=True)

        async with _core.open_nursery() as nursery:
            nursery.start_soon(child)
            await wait_all_tasks_blocked()
            nursery.cancel_scope.cancel()

    _core.run(main)

    q1.put(None)
    # This makes sure:
    # - the thread actually ran
    # - that thread has finished before we check for its output
    thread = q2.get()
    while thread.is_alive():
        time.sleep(0.01)  # pragma: no cover

    # Make sure we don't have a "Exception in thread ..." dump to the console:
    out, err = capfd.readouterr()
    assert "Exception in thread" not in out
    assert "Exception in thread" not in err


@pytest.mark.parametrize("MAX", [3, 5, 10])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("use_default_limiter", [False, True])
async def test_run_in_worker_thread_limiter(MAX, cancel, use_default_limiter):
    # This test is a bit tricky. The goal is to make sure that if we set
    # limiter=CapacityLimiter(MAX), then in fact only MAX threads are ever
    # running at a time, even if there are more concurrent calls to
    # to_thread_run_sync, and even if some of those are cancelled. And
    # also to make sure that the default limiter actually limits.
    COUNT = 2 * MAX
    gate = threading.Event()
    lock = threading.Lock()
    if use_default_limiter:
        c = current_default_thread_limiter()
        orig_total_tokens = c.total_tokens
        c.total_tokens = MAX
        limiter_arg = None
    else:
        c = CapacityLimiter(MAX)
        orig_total_tokens = MAX
        limiter_arg = c
    try:
        # We used to use regular variables and 'nonlocal' here, but it turns
        # out that it's not safe to assign to closed-over variables that are
        # visible in multiple threads, at least as of CPython 3.6 and PyPy
        # 5.8:
        #
        #   https://bugs.python.org/issue30744
        #   https://bitbucket.org/pypy/pypy/issues/2591/
        #
        # Mutating them in-place is OK though (as long as you use proper
        # locking etc.).
        class state:
            pass

        state.ran = 0
        state.high_water = 0
        state.running = 0
        state.parked = 0

        token = _core.current_trio_token()

        def thread_fn(cancel_scope):
            print("thread_fn start")
            from_thread_run_sync(cancel_scope.cancel, trio_token=token)
            with lock:
                state.ran += 1
                state.running += 1
                state.high_water = max(state.high_water, state.running)
                # The Trio thread below watches this value and uses it as a
                # signal that all the stats calculations have finished.
                state.parked += 1
            gate.wait()
            with lock:
                state.parked -= 1
                state.running -= 1
            print("thread_fn exiting")

        async def run_thread(event):
            with _core.CancelScope() as cancel_scope:
                await to_thread_run_sync(
                    thread_fn, cancel_scope, limiter=limiter_arg, cancellable=cancel
                )
            print("run_thread finished, cancelled:", cancel_scope.cancelled_caught)
            event.set()

        async with _core.open_nursery() as nursery:
            print("spawning")
            events = []
            for i in range(COUNT):
                events.append(Event())
                nursery.start_soon(run_thread, events[-1])
                await wait_all_tasks_blocked()
            # In the cancel case, we in particular want to make sure that the
            # cancelled tasks don't release the semaphore. So let's wait until
            # at least one of them has exited, and that everything has had a
            # chance to settle down from this, before we check that everyone
            # who's supposed to be waiting is waiting:
            if cancel:
                print("waiting for first cancellation to clear")
                await events[0].wait()
                await wait_all_tasks_blocked()
            # Then wait until the first MAX threads are parked in gate.wait(),
            # and the next MAX threads are parked on the semaphore, to make
            # sure no-one is sneaking past, and to make sure the high_water
            # check below won't fail due to scheduling issues. (It could still
            # fail if too many threads are let through here.)
            while state.parked != MAX or c.statistics().tasks_waiting != MAX:
                await sleep(0.01)  # pragma: no cover
            # Then release the threads
            gate.set()

        assert state.high_water == MAX

        if cancel:
            # Some threads might still be running; need to wait to them to
            # finish before checking that all threads ran. We can do this
            # using the CapacityLimiter.
            while c.borrowed_tokens > 0:
                await sleep(0.01)  # pragma: no cover

        assert state.ran == COUNT
        assert state.running == 0
    finally:
        c.total_tokens = orig_total_tokens


async def test_run_in_worker_thread_custom_limiter():
    # Basically just checking that we only call acquire_on_behalf_of and
    # release_on_behalf_of, since that's part of our documented API.
    record = []

    class CustomLimiter:
        async def acquire_on_behalf_of(self, borrower):
            record.append("acquire")
            self._borrower = borrower

        def release_on_behalf_of(self, borrower):
            record.append("release")
            assert borrower == self._borrower

    await to_thread_run_sync(lambda: None, limiter=CustomLimiter())
    assert record == ["acquire", "release"]


async def test_run_in_worker_thread_limiter_error():
    record = []

    class BadCapacityLimiter:
        async def acquire_on_behalf_of(self, borrower):
            record.append("acquire")

        def release_on_behalf_of(self, borrower):
            record.append("release")
            raise ValueError

    bs = BadCapacityLimiter()

    with pytest.raises(ValueError) as excinfo:
        await to_thread_run_sync(lambda: None, limiter=bs)
    assert excinfo.value.__context__ is None
    assert record == ["acquire", "release"]
    record = []

    # If the original function raised an error, then the semaphore error
    # chains with it
    d = {}
    with pytest.raises(ValueError) as excinfo:
        await to_thread_run_sync(lambda: d["x"], limiter=bs)
    assert isinstance(excinfo.value.__context__, KeyError)
    assert record == ["acquire", "release"]


async def test_run_in_worker_thread_fail_to_spawn(monkeypatch):
    # Test the unlikely but possible case where trying to spawn a thread fails
    def bad_start(self, *args):
        raise RuntimeError("the engines canna take it captain")

    monkeypatch.setattr(_core._thread_cache.ThreadCache, "start_thread_soon", bad_start)

    limiter = current_default_thread_limiter()
    assert limiter.borrowed_tokens == 0

    # We get an appropriate error, and the limiter is cleanly released
    with pytest.raises(RuntimeError) as excinfo:
        await to_thread_run_sync(lambda: None)  # pragma: no cover
    assert "engines" in str(excinfo.value)

    assert limiter.borrowed_tokens == 0


async def test_trio_to_thread_run_sync_token():
    # Test that to_thread_run_sync automatically injects the current trio token
    # into a spawned thread
    def thread_fn():
        callee_token = from_thread_run_sync(_core.current_trio_token)
        return callee_token

    caller_token = _core.current_trio_token()
    callee_token = await to_thread_run_sync(thread_fn)
    assert callee_token == caller_token


async def test_trio_to_thread_run_sync_expected_error():
    # Test correct error when passed async function
    async def async_fn():  # pragma: no cover
        pass

    with pytest.raises(TypeError, match="expected a sync function"):
        await to_thread_run_sync(async_fn)


trio_test_contextvar = contextvars.ContextVar("trio_test_contextvar")


async def test_trio_to_thread_run_sync_contextvars():
    trio_thread = threading.current_thread()
    trio_test_contextvar.set("main")

    def f():
        value = trio_test_contextvar.get()
        sniffio_cvar_value = current_async_library_cvar.get()
        return (value, sniffio_cvar_value, threading.current_thread())

    value, sniffio_cvar_value, child_thread = await to_thread_run_sync(f)
    assert value == "main"
    assert sniffio_cvar_value == None
    assert child_thread != trio_thread

    def g():
        parent_value = trio_test_contextvar.get()
        trio_test_contextvar.set("worker")
        inner_value = trio_test_contextvar.get()
        sniffio_cvar_value = current_async_library_cvar.get()
        return (parent_value, inner_value, sniffio_cvar_value, threading.current_thread())

    parent_value, inner_value, sniffio_cvar_value, child_thread = await to_thread_run_sync(g)
    current_value = trio_test_contextvar.get()
    sniffio_outer_value = current_async_library_cvar.get()
    assert parent_value == "main"
    assert inner_value == "worker"
    assert (
        current_value == "main"
    ), "The contextvar value set on the worker would not propagate back to the main thread"
    assert sniffio_cvar_value is None
    assert sniffio_outer_value == "trio"



async def test_trio_from_thread_run_sync():
    # Test that to_thread_run_sync correctly "hands off" the trio token to
    # trio.from_thread.run_sync()
    def thread_fn():
        trio_time = from_thread_run_sync(_core.current_time)
        return trio_time

    trio_time = await to_thread_run_sync(thread_fn)
    assert isinstance(trio_time, float)

    # Test correct error when passed async function
    async def async_fn():  # pragma: no cover
        pass

    def thread_fn():
        from_thread_run_sync(async_fn)

    with pytest.raises(TypeError, match="expected a sync function"):
        await to_thread_run_sync(thread_fn)


async def test_trio_from_thread_run():
    # Test that to_thread_run_sync correctly "hands off" the trio token to
    # trio.from_thread.run()
    record = []

    async def back_in_trio_fn():
        _core.current_time()  # implicitly checks that we're in trio
        record.append("back in trio")

    def thread_fn():
        record.append("in thread")
        from_thread_run(back_in_trio_fn)

    await to_thread_run_sync(thread_fn)
    assert record == ["in thread", "back in trio"]

    # Test correct error when passed sync function
    def sync_fn():  # pragma: no cover
        pass

    with pytest.raises(TypeError, match="appears to be synchronous"):
        await to_thread_run_sync(from_thread_run, sync_fn)


async def test_trio_from_thread_token():
    # Test that to_thread_run_sync and spawned trio.from_thread.run_sync()
    # share the same Trio token
    def thread_fn():
        callee_token = from_thread_run_sync(_core.current_trio_token)
        return callee_token

    caller_token = _core.current_trio_token()
    callee_token = await to_thread_run_sync(thread_fn)
    assert callee_token == caller_token


async def test_trio_from_thread_token_kwarg():
    # Test that to_thread_run_sync and spawned trio.from_thread.run_sync() can
    # use an explicitly defined token
    def thread_fn(token):
        callee_token = from_thread_run_sync(_core.current_trio_token, trio_token=token)
        return callee_token

    caller_token = _core.current_trio_token()
    callee_token = await to_thread_run_sync(thread_fn, caller_token)
    assert callee_token == caller_token


async def test_from_thread_no_token():
    # Test that a "raw call" to trio.from_thread.run() fails because no token
    # has been provided

    with pytest.raises(RuntimeError):
        from_thread_run_sync(_core.current_time)


async def test_trio_from_thread_run_sync_contextvars():
    trio_test_contextvar.set("main")

    def thread_fn():
        thread_parent_value = trio_test_contextvar.get()
        trio_test_contextvar.set("worker")
        thread_current_value = trio_test_contextvar.get()

        def back_in_main():
            back_parent_value = trio_test_contextvar.get()
            trio_test_contextvar.set("back_in_main")
            back_current_value = trio_test_contextvar.get()
            return back_parent_value, back_current_value

        back_parent_value, back_current_value = from_thread_run_sync(back_in_main)
        thread_after_value = trio_test_contextvar.get()
        return (
            thread_parent_value,
            thread_current_value,
            thread_after_value,
            back_parent_value,
            back_current_value,
        )

    (
        thread_parent_value,
        thread_current_value,
        thread_after_value,
        back_parent_value,
        back_current_value,
    ) = await to_thread_run_sync(thread_fn)
    current_value = trio_test_contextvar.get()
    assert current_value == thread_parent_value == "main"
    assert thread_current_value == back_parent_value == thread_after_value == "worker"
    assert back_current_value == "back_in_main"


async def test_trio_from_thread_run_contextvars():
    trio_test_contextvar.set("main")

    def thread_fn():
        thread_parent_value = trio_test_contextvar.get()
        trio_test_contextvar.set("worker")
        thread_current_value = trio_test_contextvar.get()

        async def async_back_in_main():
            back_parent_value = trio_test_contextvar.get()
            trio_test_contextvar.set("back_in_main")
            back_current_value = trio_test_contextvar.get()
            return back_parent_value, back_current_value

        back_parent_value, back_current_value = from_thread_run(async_back_in_main)
        thread_after_value = trio_test_contextvar.get()
        return (
            thread_parent_value,
            thread_current_value,
            thread_after_value,
            back_parent_value,
            back_current_value,
        )

    (
        thread_parent_value,
        thread_current_value,
        thread_after_value,
        back_parent_value,
        back_current_value,
    ) = await to_thread_run_sync(thread_fn)
    current_value = trio_test_contextvar.get()
    assert current_value == thread_parent_value == "main"
    assert thread_current_value == back_parent_value == thread_after_value == "worker"
    assert back_current_value == "back_in_main"


def test_run_fn_as_system_task_catched_badly_typed_token():
    with pytest.raises(RuntimeError):
        from_thread_run_sync(_core.current_time, trio_token="Not TrioTokentype")


async def test_from_thread_inside_trio_thread():
    def not_called():  # pragma: no cover
        assert False

    trio_token = _core.current_trio_token()
    with pytest.raises(RuntimeError):
        from_thread_run_sync(not_called, trio_token=trio_token)


@pytest.mark.skipif(buggy_pypy_asyncgens, reason="pypy 7.2.0 is buggy")
def test_from_thread_run_during_shutdown():
    save = []
    record = []

    async def agen():
        try:
            yield
        finally:
            with pytest.raises(_core.RunFinishedError), _core.CancelScope(shield=True):
                await to_thread_run_sync(from_thread_run, sleep, 0)
            record.append("ok")

    async def main():
        save.append(agen())
        await save[-1].asend(None)

    _core.run(main)
    assert record == ["ok"]


async def test_trio_token_weak_referenceable():
    token = current_trio_token()
    assert isinstance(token, TrioToken)
    weak_reference = weakref.ref(token)
    assert token is weak_reference()
