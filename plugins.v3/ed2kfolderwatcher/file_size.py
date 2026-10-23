import re
from decimal import Decimal, ROUND_CEILING


def minimum_file_bytes(value: str) -> int:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("最小文件大小格式无效，请填写如100M、1.5G，留空或0不限制")
    value = value.strip()
    if not value:
        return 0
    match = re.fullmatch(r"(\d{1,16}(?:\.\d{1,8})?)\s*(B|[KMGT](?:I?B)?)?", value, re.IGNORECASE)
    if not match:
        raise ValueError("最小文件大小格式无效，请填写如100M、1.5G，留空或0不限制")
    unit = (match[2] or "B")[0].upper()
    size = Decimal(match[1]) * (1024 ** ("BKMGT".index(unit) if unit else 0))
    if size > 1024 ** 5:
        raise ValueError("最小文件大小不能超过1024T")
    return int(size.to_integral_value(rounding=ROUND_CEILING))
