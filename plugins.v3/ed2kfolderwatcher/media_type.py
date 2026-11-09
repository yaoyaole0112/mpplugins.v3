import re


def media_kind(name, path=""):
    text = str(name)
    if not re.search(r"(?i)\.(?:mkv|mp4|avi|mov|m4v|ts|m2ts|mpg|mpeg|wmv|flv|webm|iso)$", text):
        return "unknown"
    if re.search(
        r"(?i)(?:^|[. _\-\[])S\d{1,3}(?:E\d{1,4})?(?=[. _\-\]]|$)"
        r"|(?:^|[. _\-\[])E(?:P)?\d{1,4}(?=[. _\-\]]|$)"
        r"|(?:^|[. _\-\[])\d{1,2}x\d{1,3}(?=[. _\-\]]|$)"
        r"|第\s*[零〇一二三四五六七八九十百\d]+\s*[集期季话話]"
        r"|(?:19|20)\d{2}[.\-_]?(?:0[1-9]|1[0-2])[.\-_]?(?:0[1-9]|[12]\d|3[01])",
        text,
    ):
        return "series"
    directories = str(path).replace("\\", "/").casefold().split("/")[:-1]
    if any(re.search(r"电视剧|剧集|综艺|动漫|动画|番剧|series|tv.?shows|season", part) for part in directories):
        return "series"
    if re.search(r"(?i)(?:^|[. _-])-\s*\d{1,4}(?:v\d)?(?:[. _\-\[]|$)", text):
        return "series"
    if any(re.search(r"电影|movies|films", part) for part in directories):
        return "movie"
    if re.search(r"(?i)[(（](?:19|20)\d{2}[)）]\s*[(（]tmdbid=\d+[)）]", text):
        return "movie"
    return "unknown"
