"""
自动文件归档工具
根据文件创建时间，自动将源文件夹中的文件移动到 年份-月份 归档目录。
支持多组归档任务，配置通过 config.yaml 管理。
"""

import argparse
import os
import sys
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import yaml
import requests
from loguru import logger


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.yaml") -> dict:
    """加载并校验配置文件。"""
    path = Path(config_path)
    if not path.exists():
        logger.error(f"配置文件不存在: {path.resolve()}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not cfg or "groups" not in cfg or not cfg["groups"]:
        logger.error("配置文件中未定义任何归档组 (groups)。")
        sys.exit(1)

    for i, g in enumerate(cfg["groups"]):
        for key in ("name", "archive_root"):
            if key not in g:
                logger.error(f"第 {i + 1} 组缺少必要字段: {key}")
                sys.exit(1)
        source_folder = g.get("source_folder")
        if source_folder == "":
            logger.error(f"第 {i + 1} 组 source_folder 为空字符串，请填写有效的源目录路径")
            sys.exit(1)

    return cfg


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def setup_logging(cfg: dict, log_file: str = None):
    """根据配置初始化 loguru 日志。"""
    # 移除默认的 stderr handler
    logger.remove()

    # 控制台输出
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green>  {message}",
        level="INFO",
    )

    # 文件输出: 命令行参数优先，其次配置文件
    file_path = log_file or (cfg.get("log_file") if cfg.get("log_enabled", True) else None)
    if file_path:
        logger.add(
            file_path,
            format="{time:YYYY-MM-DD HH:mm:ss}  {message}",
            level="INFO",
            encoding="utf-8",
            rotation="10 MB",
            retention="30 days",
        )


# ---------------------------------------------------------------------------
# 归档逻辑
# ---------------------------------------------------------------------------

def get_unique_path(target: Path) -> Path:
    """
    若目标路径已存在同名文件，自动在文件名后追加序号。
    report.txt -> report(1).txt -> report(2).txt -> ...
    """
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def get_file_create_time(filepath: Path) -> datetime:
    """获取文件创建时间；若平台不支持则回退到修改时间。"""
    stat = filepath.stat()
    ts = getattr(stat, "st_birthtime", None)
    if ts is None:
        # 旧版 Python 或不支持的平台，回退到 st_ctime（元数据变更时间）
        ts = stat.st_ctime
    return datetime.fromtimestamp(ts)


def archive_group(group: dict, cfg: dict) -> int:
    """
    执行单组归档任务，返回已移动的文件数。
    """
    name = group["name"]
    source_folder = group.get("source_folder")
    dst_root = Path(group["archive_root"]).resolve()
    folder_fmt = cfg.get("folder_format", "%Y-%m")
    recursive = cfg.get("scan_subfolders", False)

    logger.info(f"===== 开始处理: {name} =====")
    logger.info(f"归档根目录: {dst_root}")

    if source_folder is None:
        logger.info("未配置源目录，跳过移动阶段，直接同步。")
        return 0

    src = Path(source_folder).resolve()
    logger.info(f"源目录: {src}")

    if not src.exists():
        logger.warning(f"源目录不存在，跳过: {src}")
        return 0

    if not dst_root.exists():
        dst_root.mkdir(parents=True, exist_ok=True)
        logger.info(f"已创建归档根目录: {dst_root}")

    # 收集文件
    if recursive:
        files = [p for p in src.rglob("*") if p.is_file()]
    else:
        files = [p for p in src.glob("*") if p.is_file()]

    if not files:
        logger.info("源目录中没有文件，跳过。")
        return 0

    moved = 0
    for fp in files:
        try:
            create_time = get_file_create_time(fp)
            month_dir = dst_root / create_time.strftime(folder_fmt)
            month_dir.mkdir(parents=True, exist_ok=True)

            target = get_unique_path(month_dir / fp.relative_to(src))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(fp), str(target))
            moved += 1
            logger.info(f"[移动] {fp.name} -> {target.relative_to(dst_root)}")
        except Exception as e:
            logger.error(f"[失败] {fp.name}: {e}")

    logger.info(f"完成: {name}，共移动 {moved} 个文件。")
    return moved


# ---------------------------------------------------------------------------
# 同步逻辑
# ---------------------------------------------------------------------------

def run_sync(group: dict, cfg: dict):
    """
    归档完成后执行同步命令，自动同步本月和上月的文件夹。
    sync_command 中的占位符会被替换:
      {archive_root} — 归档根目录路径
      {name}         — 组名
      {folder}       — 月份文件夹名 (如 2026-06)
    """
    sync_tpls = group.get("sync_command")
    if not sync_tpls:
        return

    name = group["name"]
    archive_root = Path(group["archive_root"]).resolve()
    folder_fmt = cfg.get("folder_format", "%Y-%m")

    now = datetime.now()
    # 本月
    current_folder = now.strftime(folder_fmt)
    # 上月: 用当月1号减一天得到上个月
    from datetime import timedelta
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime(folder_fmt)

    folders = [current_folder, last_month]

    for folder in folders:
        month_path = archive_root / folder
        if not month_path.exists():
            logger.info(f"[同步] 跳过 {name}/{folder}，目录不存在。")
            continue

        for sync_tpl in sync_tpls:
            cmd = sync_tpl.format(
                archive_root=archive_root,
                name=name,
                folder=folder,
            )
            logger.info(f"[同步] {name}/{folder}: {cmd}")
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    logger.info(f"[同步] {name}/{folder} 完成。")
                else:
                    logger.error(f"[同步] {name}/{folder} 失败 (exit {result.returncode}): {result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                logger.error(f"[同步] {name}/{folder} 超时 (300s)。")
            except Exception as e:
                logger.error(f"[同步] {name}/{folder} 异常: {e}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="自动文件归档工具：根据文件创建时间，自动将源文件夹中的文件移动到 年份-月份 归档目录。",
    )
    parser.add_argument(
        "-c", "--config",
        required=True,
        help="指定配置文件路径",
        metavar="FILE",
    )
    parser.add_argument(
        "-l", "--log-file",
        default=None,
        help="指定日志输出文件路径（不指定则仅输出到控制台）",
        metavar="FILE",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config_path = args.config
    cfg = load_config(config_path)
    setup_logging(cfg, log_file=args.log_file)

    logger.info(f"自动归档启动，配置文件: {Path(config_path).resolve()}")
    logger.info(f"共 {len(cfg['groups'])} 组任务待执行。")

    total_moved = 0
    for group in cfg["groups"]:
        total_moved += archive_group(group, cfg)
        run_sync(group, cfg)
        logger.info("")  # 空行分隔

    logger.info(f"全部完成，共移动 {total_moved} 个文件。\n")

    heartbeat_url = cfg.get("heartbeat_url")
    if heartbeat_url:
        try:
            requests.get(heartbeat_url, timeout=10)
            logger.info(f"[心跳] 请求成功: {heartbeat_url}")
        except Exception as e:
            logger.error(f"[心跳] 请求失败: {heartbeat_url} — {e}")


if __name__ == "__main__":
    main()
