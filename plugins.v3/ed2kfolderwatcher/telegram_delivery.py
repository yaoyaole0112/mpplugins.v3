import hashlib
import json
import threading


class TelegramDeliveryStore:
    channels = ("telegram", "ordinary_upload_telegram")

    def __init__(self, path, atomic_write):
        self.path = path
        self.atomic_write = atomic_write
        self.lock = threading.RLock()
        self.dirty = False
        try:
            self.links = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.links = {}
        if not isinstance(self.links, dict):
            raise ValueError("Telegram投递凭据格式无效")
        for digest, channels in self.links.items():
            if not isinstance(digest, str) or not isinstance(channels, dict):
                raise ValueError("Telegram投递凭据格式无效")
            for channel, entry in channels.items():
                if (
                    channel not in self.channels or not isinstance(entry, dict)
                    or any(
                        not isinstance(entry.get(field), dict)
                        or any(not isinstance(key, str) or value is not True
                               for key, value in entry[field].items())
                        for field in ("deliveries", "targets")
                    )
                ):
                    raise ValueError("Telegram投递凭据格式无效")

    @staticmethod
    def digest(record):
        link = record.get("link")
        return hashlib.sha256(link.encode("utf-8")).hexdigest() if isinstance(link, str) and link else None

    def restore(self, record, channel):
        entry = self.links.get(self.digest(record), {}).get(channel, {})
        deliveries = record.setdefault(f"{channel}_deliveries", {})
        deliveries.update(entry.get("deliveries", {}))
        targets = dict(entry.get("targets", {}))
        if record.get(f"{channel}_sent") and record.get(f"{channel}_target"):
            targets[record[f"{channel}_target"]] = True
        return targets

    def checkpoint(self, records, channels=None):
        with self.lock:
            for record in records:
                digest = self.digest(record)
                if digest is None:
                    continue
                for channel in channels or self.channels:
                    deliveries = {
                        key: True for key, sent in record.get(f"{channel}_deliveries", {}).items()
                        if isinstance(key, str) and sent is True
                    }
                    target = record.get(f"{channel}_target") if record.get(f"{channel}_sent") else None
                    if not deliveries and not target:
                        continue
                    entry = self.links.setdefault(digest, {}).setdefault(
                        channel, {"deliveries": {}, "targets": {}},
                    )
                    for key in deliveries:
                        if key not in entry["deliveries"]:
                            entry["deliveries"][key] = True
                            self.dirty = True
                    if target and target not in entry["targets"]:
                        entry["targets"][target] = True
                        self.dirty = True
            if self.dirty or not self.path.exists():
                self.atomic_write(self.path, json.dumps(self.links, ensure_ascii=False))
                self.dirty = False
