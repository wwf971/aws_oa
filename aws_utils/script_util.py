# helpers for the ensure/test scripts themselves, shared by all sub-projects:
# the typed delete confirmation, the preferred timestamp formats
# (time-format.md), and the step/check test reporting.

from datetime import datetime


def delete_confirm(text_warning):
    """print the warning and demand the confirm-YYYYMMDD string of today."""
    text_expected = f"confirm-{datetime.now().strftime('%Y%m%d')}"
    print(text_warning)
    text_actual = input(f"Type {text_expected} to confirm: ").strip()
    if text_actual != text_expected:
        raise SystemExit("delete cancelled: confirmation did not match")


def timestamp_make():
    """preferred display format, e.g. 20260830_04390000+09 (time-format.md)."""
    time_now = datetime.now().astimezone()
    offset_sec = int(time_now.utcoffset().total_seconds())
    offset_sign = "+" if offset_sec >= 0 else "-"
    offset_hour = abs(offset_sec) // 3600
    centisecond = time_now.microsecond // 10000
    return (
        f"{time_now.strftime('%Y%m%d_%H%M%S')}{centisecond:02d}"
        f"{offset_sign}{offset_hour:02d}"
    )


def timestamp_resource_make():
    """timestamp for aws resource names. lambda/table/role names only allow
    letters, digits, '-' and '_', so the timezone sign of the preferred time
    format is written as p (+) / m (-): e.g. 20260830_04390000p09."""
    time_now = datetime.now().astimezone()
    offset_sec = int(time_now.utcoffset().total_seconds())
    offset_sign = "p" if offset_sec >= 0 else "m"
    offset_hour = abs(offset_sec) // 3600
    centisecond = time_now.microsecond // 10000
    return (
        f"{time_now.strftime('%Y%m%d_%H%M%S')}{centisecond:02d}"
        f"{offset_sign}{offset_hour:02d}"
    )


# ------------------------------------------------------------- test reporting

CHECK_COUNT = 0


class TestFail(Exception):
    pass


def step(text):
    print(f"\n== {text}")


def check(is_ok, text):
    global CHECK_COUNT
    CHECK_COUNT += 1
    print(f"  [{'pass' if is_ok else 'FAIL'}] {text}")
    if not is_ok:
        raise TestFail(text)


def check_count():
    return CHECK_COUNT
