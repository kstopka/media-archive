"""Display counts and decimal sizes in Polish progress messages."""


def format_count(count: int) -> str:
    return f"{count:,}".replace(",", " ")


def format_size(size: int) -> str:
    for unit, scale in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if size >= scale:
            whole, fraction = divmod((size * 100 + scale // 2) // scale, 100)
            return f"{format_count(whole)},{fraction:02d} {unit}"
    return f"{format_count(size)} B"
