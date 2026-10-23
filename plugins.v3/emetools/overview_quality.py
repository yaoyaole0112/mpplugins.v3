import re
from collections import Counter


def overview_text(value):
    return str(value or '').strip()


def overview_signature(value):
    return re.sub(r'\s+', '', overview_text(value))


def overview_length(value):
    return len(overview_signature(value))


def overview_issues(value, minimum, duplicated=False):
    text = overview_text(value)
    if text in {'', '暂无简介', '暂无剧情', '暂无剧情简介', '暂无分集剧情'}:
        return ['缺失']
    issues = []
    if not re.search(r'[\u3400-\u9fff]', text) and re.search(r'[^\W\d_]', text):
        issues.append('非中文')
    if overview_length(text) < minimum:
        issues.append('过短')
    if duplicated:
        issues.append('同季重复')
    return issues


def duplicate_overviews(episodes):
    counts = Counter((item.get('ParentIndexNumber'), overview_signature(item.get('Overview')))
                     for item in episodes
                     if overview_length(item.get('Overview')) >= 10
                     and type(item.get('ParentIndexNumber')) is int)
    return {key for key, count in counts.items() if count > 1}
