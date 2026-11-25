"""跨 WSL/Ubuntu 与 Windows 的 HWCJ-02 原始 TCP 字节采集入口。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import threading
import time


def resolve_adb(explicit_path: str | None = None) -> str:
    """依次使用命令行路径、HWCJ_ADB 环境变量或 PATH 中的 adb。"""
    explicit_resolved = shutil.which(explicit_path) if explicit_path else None
    environment_path = os.environ.get("HWCJ_ADB")
    environment_resolved = shutil.which(environment_path) if environment_path else None
    candidates = [
        explicit_path,
        explicit_resolved,
        environment_path,
        environment_resolved,
        shutil.which("adb"),
        shutil.which("adb.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    raise FileNotFoundError(
        "未找到 adb。请用 --adb 指定：WSL 可写 /mnt/d/.../adb.exe，"
        "Windows 可写 D:\\\\...\\\\adb.exe。"
    )


def check_adb(adb_path: str) -> dict[str, object]:
    """确认 ADB 可运行且至少有一台处于 device 状态，并返回追溯信息。"""
    version = subprocess.run(
        [adb_path, "version"], capture_output=True, text=True, timeout=10, check=False
    )
    if version.returncode != 0:
        raise RuntimeError(f"adb 无法运行: {version.stderr.strip()}")
    devices = subprocess.run(
        [adb_path, "devices"], capture_output=True, text=True, timeout=10, check=False
    )
    connected = [
        line for line in devices.stdout.splitlines()[1:] if line.strip().endswith("\tdevice")
    ]
    if not connected:
        raise RuntimeError("ADB 未发现已授权设备；请连接平板、开启 USB 调试并确认授权")
    return {
        "adb_version_first_line": version.stdout.splitlines()[0].strip(),
        "connected_device_lines": connected,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture(
    output_path: str | Path,
    adb_path: str | None = None,
    collector_ip: str = "10.10.100.254",
    collector_port: int = 8899,
    remote_busybox: str = "/data/local/tmp/busybox",
    duration_s: float | None = None,
) -> dict[str, object]:
    """通过平板上的 busybox nc 原样流式保存采集器 TCP 字节。

    文件逐块写入，不把完整抓包保存在内存中，因此长距离/大文件也可使用。
    按 Ctrl+C 或达到 ``duration_s`` 后安全结束并保留已经收到的字节。
    """
    adb = resolve_adb(adb_path)
    adb_audit = check_adb(adb)
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        adb,
        "exec-out",
        remote_busybox,
        "nc",
        collector_ip,
        str(collector_port),
    ]
    stderr_path = output.with_suffix(output.suffix + ".stderr.log")
    started = time.monotonic()
    total_bytes = 0
    timer: threading.Timer | None = None
    with stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_handle)
        if process.stdout is None:
            raise RuntimeError("无法读取 adb stdout")
        # Windows 不能可靠地对匿名管道使用 select。Timer 到时直接终止子进程，
        # 可让阻塞 read 在 Windows 与 WSL/Ubuntu 都退出，同时仍保持流式写盘。
        if duration_s is not None:
            if duration_s <= 0:
                raise ValueError("duration_s 必须大于 0")
            timer = threading.Timer(duration_s, process.kill)
            timer.daemon = True
            timer.start()
        try:
            with output.open("wb") as handle:
                try:
                    while True:
                        reader = getattr(process.stdout, "read1", process.stdout.read)
                        chunk = reader(65536)
                        if not chunk:
                            break
                        handle.write(chunk)
                        total_bytes += len(chunk)
                        print(f"已接收 {total_bytes} bytes", flush=True)
                except KeyboardInterrupt:
                    print("收到 Ctrl+C，正在保存已采集字节……", flush=True)
                finally:
                    handle.flush()
                    os.fsync(handle.fileno())
        finally:
            if timer is not None:
                timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

    if total_bytes == 0:
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        detail = f"；ADB 日志：{stderr_text}" if stderr_text else ""
        raise RuntimeError(f"未收到任何采集器字节{detail}")

    elapsed = time.monotonic() - started
    result: dict[str, float | int | str | object] = {
        "output": str(output),
        "stderr_log": str(stderr_path),
        "bytes": total_bytes,
        "elapsed_s": elapsed,
        "collector": f"{collector_ip}:{collector_port}",
        "source_sha256": _sha256_file(output),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "host_platform": platform.platform(),
        "adb_path": adb,
        "adb_audit": adb_audit,
        "remote_busybox": remote_busybox,
        "duration_requested_s": duration_s,
    }
    manifest_path = output.with_suffix(output.suffix + ".capture.json")
    result["capture_manifest"] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result
