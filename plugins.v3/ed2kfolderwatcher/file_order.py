import re


def filename_order(value):
    text = str(value)
    return tuple((1, int(part)) if part.isascii() and part.isdigit() else (0, part.casefold())
                 for part in re.split(r"([0-9]+)", text)), text
