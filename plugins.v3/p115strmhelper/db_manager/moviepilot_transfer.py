from inspect import signature
from typing import List

from app.db import DbOper
from app.db.models.transferhistory import TransferHistory

try:
    from app.sdk.utilities import cut as jieba_cut
except ImportError:
    from app.utils.jieba import cut as jieba_cut


class TransferHBOper(DbOper):
    """
    历史记录数据库操作扩展
    """

    def get_transfer_his_by_path_title(self, path: str) -> List[TransferHistory]:
        """
        通过路径查询转移记录
        所有匹配项

        :param path (str): 查询路径

        :return List: 数据列表
        """
        words = jieba_cut(path, HMM=False)
        title = "%".join(words)

        def query(db):
            count_kwargs = {"title": title}
            list_kwargs = {"title": title, "page": 1}
            if "wildcard" in signature(TransferHistory.count_by_title).parameters:
                count_kwargs["wildcard"] = True
            if "wildcard" in signature(TransferHistory.list_by_title).parameters:
                list_kwargs["wildcard"] = True
            total = TransferHistory.count_by_title(db, **count_kwargs)
            if not total:
                return []
            return TransferHistory.list_by_title(db, count=total, **list_kwargs)

        executor = getattr(self, "_execute_sync_query", None)
        if callable(executor):
            return executor(query)
        return query(self._db)
