import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path

from user_account import TelegramUserAccount, UserAccountError


def main():
    parser = argparse.ArgumentParser(description="ED2K 插件独立个人账号授权；验证码及密码不写入配置或文件")
    parser.add_argument("--data-dir", type=Path, required=True, help="MoviePilot 的 Ed2kFolderWatcher 数据目录，不能是插件源码目录")
    parser.add_argument("--status", action="store_true", help="仅验证已有登录，不发消息")
    arguments = parser.parse_args()
    if arguments.data_dir.resolve().is_relative_to(Path(__file__).resolve().parent):
        parser.error("数据目录不能放在源码目录中，以免凭证被上传")
    if not arguments.status and not sys.stdin.isatty():
        parser.error("登录需要交互终端，请使用 docker exec -it；不要把验证码放到命令参数或聊天中")
    try:
        from app.runtime.settings import get_runtime_setting
        proxy = get_runtime_setting("PROXY")
    except ImportError:
        proxy = None
    account = TelegramUserAccount(arguments.data_dir, proxy=proxy)
    try:
        if arguments.status:
            result = account.check()
            print("个人账号授权有效；用户 ID：", result["user_id"])
            return 0
        print("请使用你自己在 my.telegram.org 创建的 API ID/API Hash。只在当前终端输入，切勿发到聊天中。")
        if account.configured():
            credentials = account._credentials()
            print("发现已有本地凭证，将复用独立 Session；不会导入其他插件的账号。")
        else:
            api_id = input("API ID：").strip()
            api_hash = getpass.getpass("API Hash（隐藏输入）：").strip()
            if not api_id.isdigit() or int(api_id) <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
                raise UserAccountError("API ID 或 API Hash 格式错误")
            credentials = {"api_id": int(api_id), "api_hash": api_hash}
        with account.exclusive():
            user_id = asyncio.run(account.login(credentials,
                phone=lambda: getpass.getpass("手机号（含国家区号，隐藏输入）："),
                code=lambda: getpass.getpass("Telegram 登录验证码（隐藏输入）："),
                password=lambda: getpass.getpass("两步验证密码（如需要，隐藏输入）：")))
        print("授权成功；用户 ID：", user_id)
        print("会话仅保存在插件数据目录 telegram-user 中；请勿分享或上传。可回插件保存配置并立即扫描一次补发。")
        return 0
    except UserAccountError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消授权。", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"授权失败（{type(error).__name__}）；请核对验证码、两步验证及网络。敏感异常详情不输出。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
