import secrets

from config.constants import LABEL_ORDER


def level_progress(xp: int) -> tuple[int, int, int]:
    level = 1
    total = 0
    threshold = 100
    while xp >= total + threshold:
        total += threshold
        level += 1
        threshold = 100 + level * 150
    into_level = xp - total
    return level, into_level, threshold


def new_short_id() -> str:
    return secrets.token_hex(2)


def sort_tasks(tasks: list) -> list:
    def key(t):
        return (t.get("due", ""), LABEL_ORDER.get(t.get("label", "idea"), 9))
    return sorted(tasks, key=key)


def sort_tasks_by_label_then_due(tasks: list) -> list:
    def key(t):
        return (LABEL_ORDER.get(t.get("label", "idea"), 9), t.get("due", ""))
    return sorted(tasks, key=key)


def pluralize_uk(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if 2 <= n_abs % 10 <= 4 and not (12 <= n_abs % 100 <= 14):
        return few
    return many


def chunk_list(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]