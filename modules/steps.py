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
    and never reach the main thread; they are turned into logged failures."""
    results = {}

    def worker(name, func):
        # Every started lane gets a result: anything that escapes run_step (such as
        # SystemExit from sys.exit() inside a step) is recorded as a failure.
        results[name] = False
        try:
            results[name] = run_step(name, func)
        except BaseException:
            logging.exception(f"{name} ended abnormally.")

    threads = [threading.Thread(target=worker, args=(name, func), name=name) for name, func in steps]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results
