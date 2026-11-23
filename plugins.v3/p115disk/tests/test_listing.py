from unittest import TestCase
from unittest.mock import Mock, patch

from app.plugins.p115disk import P115Disk, p115_api
from app.schemas import FileItem
from app.schemas.exception import StorageQueryError


STORAGE = "115网盘Plus"


class ListingTests(TestCase):
    def test_plugin_dispatches_only_its_storage(self):
        plugin = object.__new__(P115Disk)
        plugin._disk_name = STORAGE
        plugin._enabled = True
        plugin._p115_api = Mock()
        plugin._p115_api.list.return_value = []
        root = FileItem(storage=STORAGE, path="/", type="dir")
        self.assertIsNone(plugin.list_files(root.model_copy(update={"storage": "u115"})))
        plugin._p115_api.list.assert_not_called()
        self.assertEqual(plugin.list_files(root), [])
        plugin._p115_api.list.assert_called_once_with(root)
        plugin._p115_api.iter_files.return_value = None
        with self.assertRaises(StorageQueryError):
            plugin.list_files(root, recursion=True)
        plugin._enabled = False
        with self.assertRaises(StorageQueryError):
            plugin.list_files(root)

    def test_root_distinguishes_empty_directory_from_failed_request(self):
        api = p115_api.P115Api(Mock(), STORAGE)
        api._list_rate_limiter = Mock()
        root = FileItem(storage=STORAGE, path="/", type="dir")
        with patch.object(p115_api, "fs_files_iter", return_value=iter([{"data": []}])) as get:
            self.assertEqual(api.list(root), [])
            self.assertEqual(get.call_args.args[1], 0)
        with (
            patch.object(p115_api, "fs_files_iter", side_effect=TimeoutError),
            patch.object(p115_api, "StorageChain") as fallback,
            self.assertRaises(StorageQueryError),
        ):
            api.list(root)
        fallback.assert_not_called()

    def test_subdirectory_without_id_uses_read_only_resolution(self):
        api = p115_api.P115Api(Mock(), STORAGE)
        api._list_rate_limiter = api._get_item_rate_limiter = Mock()
        folder = FileItem(storage=STORAGE, path="/folder", type="dir")
        normalized = {
            "id": 12, "parent_id": 11, "name": "child.txt", "is_dir": False,
            "size": 4, "ctime": 10, "pickcode": "pick",
        }
        with (
            patch.object(p115_api, "get_id_to_path", return_value=11) as get_id,
            patch.object(p115_api, "fs_files_iter", return_value=iter([{"data": [{}]}])),
            patch.object(p115_api, "normalize_attr", return_value=normalized),
            patch.object(api, "get_pid_by_path") as create_capable,
        ):
            self.assertEqual(api.list(folder)[0].path, "/folder/child.txt")
        self.assertEqual(get_id.call_args.args[1], "/folder")
        self.assertFalse(get_id.call_args.kwargs["ensure_file"])
        create_capable.assert_not_called()
