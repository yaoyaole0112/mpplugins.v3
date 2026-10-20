"""Bounded, observable task coordination without replaying 115 requests."""

import copy
import threading
import time
import uuid

from app.sdk.logging import logger


class P115TaskCoordinator:
    def __init__(self):
        self._condition = threading.Condition()
        self._local = threading.local()
        self._waiting = []
        self._active = None
        self._jobs = {}
        self._closed = False

    def reopen(self):
        with self._condition:
            self._closed = False

    def close(self):
        with self._condition:
            self._closed = True
            for job in self._waiting:
                job.update(state="cancelled", message="插件已停止，等待任务已取消，未执行操作",
                           finished=time.monotonic())
                job["done"].set()
            self._waiting.clear()
            self._condition.notify_all()

    def _reserve(self, key, label, manual):
        with self._condition:
            if self._closed:
                raise ValueError("115任务协调器已停止，请刷新插件页面后重试")
            now = time.monotonic()
            self._jobs = {token: job for token, job in self._jobs.items()
                          if job["state"] in {"queued", "running"} or now - job["finished"] < 1800}
            existing = next((job for job in self._jobs.values()
                             if job["key"] == key and job["manual"] == manual
                             and job["state"] in {"queued", "running"}), None)
            if existing:
                return existing, False
            if len(self._waiting) >= 32:
                raise ValueError("115等待任务已达上限，请等待现有任务完成")
            finished = sorted((job for job in self._jobs.values() if job["state"] not in {"queued", "running"}),
                              key=lambda job: job["finished"])
            for job in finished[:-50]:
                self._jobs.pop(job["id"], None)
            job = dict(id=uuid.uuid4().hex, key=key, label=label, manual=manual, state="queued",
                       message="已排队，当前操作结束后优先执行" if manual else "等待其他115任务完成",
                       done=threading.Event(), result=None, finished=None)
            self._waiting.append(job)
            self._jobs[job["id"]] = job
            if self._active:
                logger.info("媒体增强115任务：%s已排队，当前执行%s，等待=%d，手动任务优先",
                            label, self._active["label"], len(self._waiting))
            return job, True

    def _ordered(self):
        return sorted(self._waiting, key=lambda job: not job["manual"])

    def _execute(self, job, callback):
        with self._condition:
            while job["state"] == "queued":
                if self._active is None and self._ordered()[0] is job:
                    self._waiting.remove(job)
                    self._active = job
                    job.update(state="running", message="正在执行")
                    logger.info("媒体增强115任务：开始%s，剩余等待=%d", job["label"], len(self._waiting))
                    break
                self._condition.wait()
            if job["state"] != "running":
                return {"ok": False, "message": job["message"]}
        self._local.active = True
        try:
            result = callback()
            with self._condition:
                job.update(state="failed" if isinstance(result, dict) and result.get("ok") is False else "completed",
                           message=result.get("message", "任务已完成") if isinstance(result, dict) else "任务已完成",
                           result=result)
            return result
        except Exception as error:
            logger.warning("媒体增强115协调任务失败：%s（%s），未自动重放操作", job["label"], type(error).__name__)
            with self._condition:
                job.update(state="failed", message=f"任务未完成（{type(error).__name__}），请查看日志后重新确认")
            raise
        finally:
            self._local.active = False
            with self._condition:
                job["finished"] = time.monotonic()
                logger.info("媒体增强115任务：%s结束，状态=%s", job["label"], job["state"])
                self._active = None
                job["done"].set()
                self._condition.notify_all()

    def run(self, key, label, callback, manual=False):
        if getattr(self._local, "active", False):
            return callback()
        job, created = self._reserve(key, label, manual)
        if not created:
            return {"ok": True, "queued": True, "job_id": job["id"], "message": "同类任务已排队或正在执行，未重复提交"}
        return self._execute(job, callback)

    def submit(self, key, label, callback):
        job, created = self._reserve(key, label, True)
        if created:
            def worker():
                try:
                    self._execute(job, callback)
                except Exception:
                    pass
            try:
                threading.Thread(target=worker, name="emetools-115-manual", daemon=True).start()
            except Exception:
                with self._condition:
                    self._waiting.remove(job)
                    job.update(state="failed", message="无法启动任务，未执行操作", finished=time.monotonic())
                    job["done"].set()
                    self._condition.notify_all()
                raise
        return job

    def job_status(self, token):
        with self._condition:
            job = self._jobs.get(token)
            if job and job["finished"] is not None and time.monotonic() - job["finished"] >= 1800:
                self._jobs.pop(token, None)
                job = None
            if not job:
                return {"ok": False, "state": "expired", "message": "任务记录已失效，请核实结果，不要重复提交删除或转存"}
            result = {key: job[key] for key in ("id", "label", "state", "message")}
            if job["state"] in {"completed", "failed"}:
                result["result"] = copy.deepcopy(job["result"])
            return result

    def status(self):
        with self._condition:
            jobs = ([self._active] if self._active else []) + self._ordered()
            return [{key: job[key] for key in ("id", "label", "state", "message")} for job in jobs]
