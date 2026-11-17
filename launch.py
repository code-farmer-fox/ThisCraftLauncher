import argparse
import os
import subprocess
import sys

import minecraft_launcher_lib as mcll
import psutil


def get_max_memory(cap_gb: int = 4) -> int:
    """根据物理内存计算分配大小（GB），上限为 cap_gb，下限为 1。"""
    total_gb = psutil.virtual_memory().total // (1024 ** 3)
    return max(1, min(total_gb, cap_gb))


def options_create(username: str, memory: int, game_dir: str) -> dict:
    return {
        "username": username,
        "uuid": "000000000000-0000-00000000",
        "token": "mojang",
        "jvmArguments": [f"-Xms{memory}G", f"-Xmx{memory}G"],
        "gameDirectory": os.path.abspath(game_dir),
    }


def build_command(version: str, options: dict, game_dir: str) -> list[str]:
    return mcll.command.get_minecraft_command(version, game_dir, options)


def launch(version: str, username: str, game_dir: str, memory: int | None) -> int:
    game_dir = os.path.abspath(game_dir)
    if not os.path.isdir(game_dir):
        print(f"[错误] 游戏目录不存在: {game_dir}", file=sys.stderr)
        return 1

    mem_gb = memory if memory is not None else get_max_memory()
    opts = options_create(username, mem_gb, game_dir)

    try:
        cmd = build_command(version, opts, game_dir)
    except Exception as e:
        print(f"[错误] 生成启动命令失败: {e}", file=sys.stderr)
        return 1

    print(f"[信息] 版本: {version}  用户: {username}  内存: {mem_gb}G")
    print("[信息] 启动命令:", " ".join(cmd))

    try:
        process = subprocess.Popen(
            cmd,
            cwd=game_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        print("[错误] 找不到 Java，请确认已安装并加入 PATH。", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[错误] 启动失败: {e}", file=sys.stderr)
        return 1

    print(f"[信息] Minecraft 已启动，PID: {process.pid}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mc-launcher",
        description="使用 minecraft-launcher-lib 启动 Minecraft（离线模式）",
    )
    parser.add_argument("version", help="要启动的游戏版本，例如 1.20.1")
    parser.add_argument(
        "-u", "--username",
        default="Steve",
        help="玩家用户名（离线模式），默认 Steve",
    )
    parser.add_argument(
        "-d", "--game-dir",
        default=".minecraft",
        help=".minecraft 目录路径，默认 ./.minecraft",
    )
    parser.add_argument(
        "-m", "--memory",
        type=int,
        default=None,
        metavar="GB",
        help="最大内存（GB），默认根据物理内存自动计算（上限 4G）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.memory is not None and args.memory < 1:
        print("[错误] --memory 必须 >= 1", file=sys.stderr)
        return 2
    return launch(args.version, args.username, args.game_dir, args.memory)


if __name__ == "__main__":
    sys.exit(main())