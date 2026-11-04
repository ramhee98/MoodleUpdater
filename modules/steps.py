import logging
import threading


def run_step(name, func, *args, **kwargs):
    """Run one update step and return True on success.

    A step signals failure by returning a falsy value. Unexpected exceptions are
    logged with their traceback and also count as failure, so a crash can never
    be mistaken for success by the steps that follow."""
    try:
        ok = func(*args, **kwargs)
    except Exception:
        logging.exception(f"{name} failed with an unexpected error.")
        return False
    if not ok:
        logging.error(f"{name} failed.")
    return bool(ok)


def run_steps_concurrently(steps):
    """Run (name, callable) pairs in parallel threads and return {name: success}.

    Exceptions raised inside a thread would otherwise only be printed to stderr
    and never reach the main thread; run_step turns them into a logged failure."""
    results = {}

    def worker(name, func):
        results[name] = run_step(name, func)

    threads = [threading.Thread(target=worker, args=(name, func), name=name) for name, func in steps]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results
