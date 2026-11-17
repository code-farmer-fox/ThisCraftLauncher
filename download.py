import json
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
RESOURCES_BASE = "https://resources.download.minecraft.net"
DEFAULT_ROOT = Path(".minecraft")
UA = "Mozilla/5.0 (Minecraft-Downloader)"


class DownloadError(Exception):
    pass


class Downloader:
    def __init__(
        self,
        version: str,
        root: str | Path = DEFAULT_ROOT,
        workers: int = 64,        # 小文件并发（libraries/assets）
        chunk_workers: int = 12,  # client.jar 分块并发
    ):
        self.version = version
        self.root = Path(root)
        self.workers = workers
        self.chunk_workers = chunk_workers
        self.manifest: dict | None = None
        self.info: dict | None = None

    # ---------- 基础：小文件下载 ----------
    @staticmethod
    def download(url: str, dest: str | Path, *, sha1: str = None, retries: int = 3, chunk_size: int = 1 << 16) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # 已存在 → 有 sha1 则校验，通过才跳过；无 sha1 退化为按大小跳过
        if dest.exists() and dest.stat().st_size > 0:
            if sha1 is None:
                logger.debug("skip existing (no sha1): %s", dest)
                return dest
            if Downloader._sha1(dest) == sha1:
                logger.debug("skip existing (sha1 ok): %s", dest)
                return dest
            logger.warning("sha1 mismatch on existing file, redownload: %s", dest)

        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(1, retries + 1):
            try:
                req = Request(url, headers={"User-Agent": UA})
                with urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
                    while chunk := resp.read(chunk_size):
                        f.write(chunk)
                # 下载后校验
                if sha1 is not None:
                    actual = Downloader._sha1(tmp)
                    if actual != sha1:
                        tmp.unlink(missing_ok=True)
                        raise DownloadError(f"sha1 mismatch for {url}: {actual} != {sha1}")
                tmp.replace(dest)
                return dest
            except DownloadError:
                # 校验失败不重试，直接抛
                raise
            except (URLError, HTTPError, TimeoutError, OSError) as e:
                logger.warning("download fail (%d/%d) %s: %s", attempt, retries, url, e)
                tmp.unlink(missing_ok=True)
                if attempt == retries:
                    raise DownloadError(f"failed to download {url}") from e
        # 理论上不可达
        raise DownloadError(f"failed to download {url}")

    # ---------- 大文件：Range 分块并发 ----------
    @staticmethod
    def download_chunked(
        url: str,
        dest: str | Path,
        *,
        parts: int = 12,
        retries: int = 3,
        expected_sha1: str | None = None,
    ) -> Path:
        """
        用 HTTP Range 把文件切 `parts` 块并行下载，合并后校验 SHA1。
        服务器不支持 Range（非 206）时自动退化为单流。
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # 已完成且校验通过 → 跳过
        if dest.exists() and dest.stat().st_size > 0:
            if expected_sha1 is None or Downloader._sha1(dest) == expected_sha1:
                logger.info("client already complete: %s", dest)
                return dest
            logger.warning("client sha1 mismatch on existing file, redownload: %s", dest)

        size, supports_range = Downloader._probe(url)
        if not supports_range or size == 0:
            logger.info("server no range support, fallback to single-stream")
            tmp = dest.with_suffix(dest.suffix + ".single")
            Downloader._stream_to(url, tmp)
            Downloader._finalize(tmp, dest, expected_sha1)
            return dest

        part_size = (size + parts - 1) // parts
        ranges = []
        for i in range(parts):
            start = i * part_size
            end = min(start + part_size - 1, size - 1)
            if start <= end:
                ranges.append((i, start, end))

        part_paths = [dest.with_suffix(dest.suffix + f".part{i}") for i, _, _ in ranges]
        lock = threading.Lock()
        done = [0]
        total = len(ranges)

        def fetch(i: int, start: int, end: int, path: Path) -> Path:
            want = end - start + 1
            if path.exists() and path.stat().st_size == want:
                with lock:
                    done[0] += 1
                    logger.info("client chunk %d/%d (cached)", done[0], total)
                return path
            for attempt in range(1, retries + 1):
                try:
                    req = Request(
                        url,
                        headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"},
                    )
                    with urlopen(req, timeout=60) as resp, open(path, "wb") as f:
                        while chunk := resp.read(1 << 16):
                            f.write(chunk)
                    got = path.stat().st_size
                    if got != want:
                        raise DownloadError(f"part {i} size mismatch {got} != {want}")
                    with lock:
                        done[0] += 1
                        logger.info("client chunk %d/%d done", done[0], total)
                    return path
                except (URLError, HTTPError, TimeoutError, OSError, DownloadError) as e:
                    logger.warning("chunk %d attempt %d/%d failed: %s", i, attempt, retries, e)
                    path.unlink(missing_ok=True)
                    if attempt == retries:
                        raise
            return path

        logger.info("client: %d bytes, %d parts, %d threads", size, total, parts)
        with ThreadPoolExecutor(max_workers=parts) as pool:
            futures = [
                pool.submit(fetch, i, s, e, p)
                for (i, s, e), p in zip(ranges, part_paths)
            ]
            for fut in as_completed(futures):
                fut.result()

        # 合并
        tmp = dest.with_suffix(dest.suffix + ".merge")
        with open(tmp, "wb") as out:
            for p in part_paths:
                with open(p, "rb") as f:
                    while chunk := f.read(1 << 20):
                        out.write(chunk)
                p.unlink(missing_ok=True)
        Downloader._finalize(tmp, dest, expected_sha1)
        return dest

    @staticmethod
    def _probe(url: str) -> tuple[int, bool]:
        try:
            req = Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
            with urlopen(req, timeout=30) as resp:
                if resp.status == 206:
                    total = int(resp.headers.get("Content-Range", "").split("/")[-1])
                    return total, True
                return int(resp.headers.get("Content-Length", "0")), False
        except (HTTPError, URLError, ValueError) as e:
            logger.warning("probe failed: %s", e)
            return 0, False

    @staticmethod
    def _stream_to(url: str, path: Path) -> None:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=60) as resp, open(path, "wb") as f:
            while chunk := resp.read(1 << 16):
                f.write(chunk)

    @staticmethod
    def _sha1(path: Path) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _finalize(tmp: Path, dest: Path, expected_sha1: str | None) -> None:
        if expected_sha1:
            actual = Downloader._sha1(tmp)
            if actual != expected_sha1:
                tmp.unlink(missing_ok=True)
                raise DownloadError(f"sha1 mismatch: {actual} != {expected_sha1}")
        tmp.replace(dest)

    # ---------- 各阶段 ----------
    def fetch_manifest(self) -> dict:
        path = self.root / "version_manifest.json"
        self.download(MANIFEST_URL, path)
        self.manifest = json.loads(path.read_text(encoding="utf-8"))
        return self.manifest

    def fetch_version_json(self) -> dict:
        if self.manifest is None:
            self.fetch_manifest()
        target = next((v for v in self.manifest["versions"] if v["id"] == self.version), None)
        if target is None:
            raise DownloadError(f"version {self.version!r} not found in manifest")
        dest = self.root / "versions" / self.version / f"{self.version}.json"
        # 按需求：version json 不做 sha1 校验
        self.download(target["url"], dest)
        self.info = json.loads(dest.read_text(encoding="utf-8"))
        return self.info

    def fetch_client(self) -> Path:
        if self.info is None:
            self.fetch_version_json()
        client = self.info["downloads"]["client"]
        dest = self.root / "versions" / self.version / f"{self.version}.jar"
        logger.info("downloading client.jar (%d bytes) in %d chunks",
                    client.get("size", 0), self.chunk_workers)
        return self.download_chunked(
            client["url"], dest,
            parts=self.chunk_workers,
            expected_sha1=client.get("sha1"),
        )

    def fetch_libraries(self) -> list[Path]:
        if self.info is None:
            self.fetch_version_json()
        tasks = []
        for lib in self.info.get("libraries", []):
            artifact = lib.get("downloads", {}).get("artifact")
            if artifact:
                tasks.append((
                    artifact["url"],
                    self.root / "libraries" / artifact["path"],
                    artifact.get("sha1"),
                ))
        return self._download_many(tasks)

    def fetch_assets(self) -> list[Path]:
        if self.info is None:
            self.fetch_version_json()
        index_id = self.info["assetIndex"]["id"]
        index_url = self.info["assetIndex"]["url"]
        index_sha1 = self.info["assetIndex"].get("sha1")
        index_path = self.root / "assets" / "indexes" / f"{index_id}.json"
        self.download(index_url, index_path, sha1=index_sha1)

        objects = json.loads(index_path.read_text(encoding="utf-8"))["objects"]
        tasks = []
        for obj in objects.values():
            h = obj["hash"]
            prefix = h[:2]
            tasks.append((
                f"{RESOURCES_BASE}/{prefix}/{h}",
                self.root / "assets" / "objects" / prefix / h,
                h,  # asset hash 即文件 sha1
            ))
        return self._download_many(tasks)

    # ---------- 并发下载（小文件）----------
    def _download_many(self, tasks: list[tuple[str, Path, str | None]]) -> list[Path]:
        results: list[Path] = []
        if not tasks:
            return results
        logger.info("downloading %d files with %d threads", len(tasks), self.workers)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self.download, url, dest, sha1=sha1): dest
                for url, dest, sha1 in tasks
            }
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except DownloadError as e:
                    logger.error("task failed: %s", e)
        return results

    # ---------- 一键全量 ----------
    def run_all(self) -> None:
        self.fetch_manifest()
        self.fetch_version_json()
        self.fetch_client()
        self.fetch_libraries()
        self.fetch_assets()
        self.fetch_manifest()
        self.fetch_version_json()
        self.fetch_client()
        self.fetch_libraries()
        self.fetch_assets()


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Minecraft version downloader")
    parser.add_argument("version")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--chunk-workers", type=int, default=12)
    args = parser.parse_args()

    Downloader(
        args.version,
        root=args.root,
        workers=args.workers,
        chunk_workers=args.chunk_workers,
    ).run_all()