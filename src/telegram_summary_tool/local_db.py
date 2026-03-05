"""
从 macOS 原生 Telegram 应用的本地加密数据库中读取聊天记录。

原理：
1. 读取 .tempkeyEncrypted (64字节) → AES-CBC 解密 → 拿到数据库密钥 + salt
2. 用 sqlcipher CLI 将加密数据库导出为临时明文数据库
3. 用标准 sqlite3 读取明文数据库，解析二进制消息

参考：
- https://gist.github.com/stek29/8a7ac0e673818917525ec4031d77a713
- https://gist.github.com/Green-m/6e3a6d2ffbb1b669d37b756572ca232f
"""

from __future__ import annotations

import binascii
import io
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

import mmh3
from Crypto.Cipher import AES
from Crypto.Hash import SHA512

from .telegram_export import TelegramMessage

# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────

# Telegram 默认本地密码（用户未设置密码时使用）
DEFAULT_PASSWORD = "no-matter-key"

# murmur3 hash 的 seed（来自 Telegram 源码）
MURMUR_SEED = -137723950

# macOS 原生 Telegram 的数据目录
TELEGRAM_CONTAINER = os.path.expanduser(
    "~/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/appstore"
)

# Peer 类型常量（PeerId 高 32 位）
PEER_TYPE_USER = 0
PEER_TYPE_GROUP = 1
PEER_TYPE_CHANNEL = 2
PEER_TYPE_SECRET = 3

# 明文数据库缓存目录
CACHE_DIR = Path(os.path.expanduser("~/.cache/telegram-summary-tool"))

# 缓存的明文数据库文件名
CACHE_DB_NAME = "plain_cache.db"


# ──────────────────────────────────────────────
# 数据库缓存管理（核心优化）
# ──────────────────────────────────────────────

class _DbCache:
    """
    明文数据库缓存管理器。

    策略：缓存文件存在就直接用（Telegram 一直在写 DB，mtime 永远更新，
    不能用 mtime 判断）。用户想要最新数据时手动点「刷新」。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._conn: Optional[sqlite3.Connection] = None
        # 群组列表结果缓存（避免每次请求都做 GROUP BY 全表扫描）
        self._chat_list_cache: Optional[List] = None

    def get_connection(self, password: str = DEFAULT_PASSWORD) -> sqlite3.Connection:
        """获取明文数据库连接。缓存存在则直接用，不存在则导出。"""
        cache_path = CACHE_DIR / CACHE_DB_NAME

        # 快速路径：内存中已有连接
        if self._conn:
            return self._conn

        # 加锁
        with self._lock:
            if self._conn:
                return self._conn

            # 缓存文件存在 → 直接打开（不重新导出）
            if cache_path.exists() and cache_path.stat().st_size > 0:
                logger.info("命中磁盘缓存: %s", cache_path)
            else:
                # 首次，需要导出
                self._do_export(password, cache_path)

            self._conn = sqlite3.connect(str(cache_path), check_same_thread=False)
            self._ready.set()
            return self._conn

    def get_chat_list_cache(self) -> Optional[List]:
        """获取群组列表缓存（命中则跳过 GROUP BY 查询）。"""
        return self._chat_list_cache

    def set_chat_list_cache(self, chats: List) -> None:
        """保存群组列表到内存缓存。"""
        self._chat_list_cache = chats

    def refresh(self, password: str = DEFAULT_PASSWORD) -> None:
        """强制刷新：重新从加密库导出（用户手动触发）。"""
        cache_path = CACHE_DIR / CACHE_DB_NAME
        with self._lock:
            # 关闭旧连接
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            # 清除群组列表缓存，刷新后需要重新查询
            self._chat_list_cache = None

            self._do_export(password, cache_path)
            self._conn = sqlite3.connect(str(cache_path), check_same_thread=False)

    def _do_export(self, password: str, cache_path: Path) -> None:
        """执行实际的数据库导出。"""
        logger.info("正在导出明文数据库...")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _, key_path = find_telegram_data()
        db_path = find_telegram_data()[0]
        db_key, db_salt = _decrypt_temp_key(key_path, password)
        _export_plaintext_db(db_path, db_key, db_salt, cache_path)
        logger.info("导出完成: %s (%.1f MB)", cache_path, cache_path.stat().st_size / 1024**2)

    def warmup(self, password: str = DEFAULT_PASSWORD) -> None:
        """预热（启动时后台调用）：建立连接 + 预计算群组列表。"""
        try:
            self.get_connection(password)
            # 预热时直接计算群组列表，页面打开即可用
            from . import local_db as _self_mod
            _self_mod.list_chats(password, min_messages=1)
            logger.info("预热完成：群组列表已缓存到内存")
        except Exception as e:
            logger.error("预热失败: %s", e)

    def wait_ready(self, timeout: float = 60) -> bool:
        return self._ready.wait(timeout=timeout)


# 全局缓存实例
_db_cache = _DbCache()


# ──────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class ChatInfo:
    """群组/频道/用户的基本信息"""
    peer_id: int          # 原始 peer ID（不含类型编码）
    peer_type: int        # 0=User, 1=Group, 2=Channel
    title: str            # 群名/用户名
    username: str         # @username
    message_count: int    # 消息总数


# ──────────────────────────────────────────────
# 第一步：解密 .tempkeyEncrypted → 数据库密钥
# ──────────────────────────────────────────────

def _decrypt_temp_key(key_file: Path, password: str = DEFAULT_PASSWORD) -> Tuple[bytes, bytes]:
    """
    解密 .tempkeyEncrypted 文件，返回 (db_key, db_salt)。

    文件结构 (64 字节):
      [0:32]  数据库密钥 (AES-256)
      [32:48] 数据库 salt
      [48:52] Murmur3 校验哈希
      [52:64] 零填充
    """
    # 读取加密数据
    data_enc = key_file.read_bytes()
    if len(data_enc) != 64:
        raise ValueError(f".tempkeyEncrypted 应为 64 字节，实际 {len(data_enc)} 字节")

    # KDF: SHA512(password) → 前 32 字节作 AES key，后 16 字节作 IV
    h = SHA512.new()
    h.update(password.encode("utf-8"))
    digest = h.digest()
    aes_key = digest[0:32]
    aes_iv = digest[-16:]

    # AES-CBC 解密
    cipher = AES.new(key=aes_key, iv=aes_iv, mode=AES.MODE_CBC)
    data = cipher.decrypt(data_enc)

    # 提取字段
    db_key = data[0:32]
    db_salt = data[32:48]
    stored_hash = struct.unpack("<i", data[48:52])[0]

    # Murmur3 完整性校验
    calc_hash = mmh3.hash(db_key + db_salt, seed=MURMUR_SEED)
    if stored_hash != calc_hash:
        raise ValueError(
            f"Murmur3 校验失败 (stored={stored_hash}, calc={calc_hash})。"
            "可能设置了本地密码，请用 password 参数传入。"
        )

    return db_key, db_salt


# ──────────────────────────────────────────────
# 第二步：用 sqlcipher 导出明文数据库
# ──────────────────────────────────────────────

def _find_sqlcipher() -> str:
    """查找 sqlcipher 可执行文件路径"""
    # 常见安装路径
    candidates = [
        "/opt/homebrew/bin/sqlcipher",    # Apple Silicon Homebrew
        "/usr/local/bin/sqlcipher",       # Intel Homebrew
        shutil.which("sqlcipher"),        # PATH 中查找
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError(
        "找不到 sqlcipher。请运行: brew install sqlcipher"
    )


def _export_plaintext_db(
    encrypted_db: Path,
    db_key: bytes,
    db_salt: bytes,
    output_path: Path,
) -> Path:
    """用 sqlcipher CLI 将加密数据库导出为明文 SQLite"""
    sqlcipher = _find_sqlcipher()
    pragma_key = binascii.hexlify(db_key + db_salt).decode("utf-8")

    # 如果输出文件已存在，先删除（sqlcipher ATTACH 不会覆盖）
    if output_path.exists():
        output_path.unlink()

    # 构建 sqlcipher 命令
    sql_commands = f"""
PRAGMA key="x'{pragma_key}'";
PRAGMA cipher_plaintext_header_size=32;
ATTACH DATABASE '{output_path}' AS plaintext KEY '';
SELECT sqlcipher_export('plaintext');
DETACH DATABASE plaintext;
"""
    proc = subprocess.run(
        [sqlcipher, str(encrypted_db)],
        input=sql_commands,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sqlcipher 导出失败:\n{proc.stderr}")

    if not output_path.exists():
        raise RuntimeError("sqlcipher 执行完毕但明文数据库未生成")

    return output_path


# ──────────────────────────────────────────────
# 第三步：解析二进制消息格式
# ──────────────────────────────────────────────

class _BinaryReader:
    """读取 Telegram 自定义二进制编码"""

    def __init__(self, data: bytes):
        self.buf = io.BytesIO(data)

    def read_fmt(self, fmt: str):
        size = struct.calcsize(fmt)
        d = self.buf.read(size)
        if len(d) < size:
            raise EOFError(f"需要 {size} 字节，剩余 {len(d)}")
        return struct.unpack("<" + fmt, d)[0]

    def read_int8(self) -> int:
        return self.read_fmt("b")

    def read_uint8(self) -> int:
        return self.read_fmt("B")

    def read_int32(self) -> int:
        return self.read_fmt("i")

    def read_uint32(self) -> int:
        return self.read_fmt("I")

    def read_int64(self) -> int:
        return self.read_fmt("q")

    def read_bytes(self) -> bytes:
        slen = self.read_int32()
        return self.buf.read(slen)

    def read_str(self) -> str:
        return self.read_bytes().decode("utf-8", errors="replace")


def _parse_message_key(key: bytes) -> Tuple[int, int, int, Optional[datetime]]:
    """
    解析 t7 消息表的 key (20 字节)。

    格式: peer_id_int64(8) + namespace(4) + timestamp(4) + msg_id(4)
    peer_id_int64 = (peer_type << 32) | actual_peer_id

    返回: (peer_type, peer_id, msg_id, datetime)
    """
    peer_raw = struct.unpack(">q", key[0:8])[0]
    # namespace = struct.unpack(">I", key[8:12])[0]  # 暂不使用
    timestamp = struct.unpack(">I", key[12:16])[0]
    msg_id = struct.unpack(">I", key[16:20])[0]

    # 拆分 peer 类型和实际 ID
    peer_type = (peer_raw >> 32) & 0xFFFFFFFF
    peer_id = peer_raw & 0xFFFFFFFF

    # 转换时间戳（使用本地时间，与前端日期选择器一致）
    dt = None
    if timestamp > 0:
        try:
            dt = datetime.fromtimestamp(timestamp)
        except (ValueError, OSError):
            pass

    return peer_type, peer_id, msg_id, dt


def _parse_message_value(value: bytes) -> Tuple[Optional[int], str, Optional[int]]:
    """
    解析 t7 消息表的 value（IntermediateMessage 二进制格式）。

    dataFlags 各 bit 对应的可选字段：
      bit 0 (0x01): GloballyUniqueId (int64)
      bit 1 (0x02): GlobalMessageIds (int64)
      bit 2 (0x04): LocalTimestamp (int32)
      bit 3 (0x08): ThreadId (int64) — Forum Topics 话题 ID
      bit 4 (0x10): 预留
      bit 5 (0x20): GroupingKey (int64) — 新版本新增

    返回: (author_id, text, thread_id)
    """
    r = _BinaryReader(value)
    try:
        r.read_int8()       # 消息类型
        r.read_uint32()     # stableId
        r.read_uint32()     # stableVersion
        data_flags = r.read_uint8()

        # 按 flag 位跳过可选字段
        if data_flags & 0x01:  # GloballyUniqueId
            r.read_int64()
        if data_flags & 0x02:  # GlobalMessageIds
            r.read_int64()
        if data_flags & 0x04:  # LocalTimestamp
            r.read_int32()
        # 提取 ThreadId（Forum Topics 话题 ID）
        thread_id = None
        if data_flags & 0x08:
            thread_id = r.read_int64()
        if data_flags & 0x10:  # 预留字段
            r.read_int64()
        if data_flags & 0x20:  # GroupingKey（新版新增）
            r.read_int64()

        r.read_uint32()     # MessageFlags
        r.read_uint32()     # MessageTags

        # 跳过 ForwardInfo
        has_fwd = r.read_int8()
        if has_fwd == 1:
            r.read_int64()   # authorId
            r.read_int32()   # date
            if r.read_int8() == 1:  # sourceId
                r.read_int64()
            if r.read_int8() == 1:  # sourceMessageId
                r.read_int32()
            if r.read_int8() == 1:  # authorSignature
                r.read_str()
            if r.read_int8() == 1:  # psaType
                r.read_str()
            r.read_uint32()  # flags

        # 读取作者 ID（存储为 PeerId 的 int64 编码）
        has_author = r.read_int8()
        author_id = r.read_int64() if has_author == 1 else None

        # 读取消息文本
        text = r.read_str()

        return author_id, text, thread_id
    except (EOFError, struct.error):
        return None, "", None


def _parse_peer_title(value: bytes) -> Tuple[str, str]:
    """
    从 t2 value 中提取群名和 username。

    t2 的 value 不是标准 PostboxDecoder 格式，
    而是自定义编码。字符串字段以 2字节tag + 0x04 + 4字节长度 + utf8 存储。
    """
    title = ""
    username = ""

    i = 0
    while i < len(value) - 5:
        if value[i] == 0x04:
            slen = struct.unpack_from("<I", value, i + 1)[0]
            if 1 <= slen <= 500 and i + 5 + slen <= len(value):
                try:
                    s = value[i + 5: i + 5 + slen].decode("utf-8")
                    if s.isprintable() and s.strip():
                        # 前 2 字节是 field tag
                        tag = value[i - 2: i].decode("ascii", errors="replace") if i >= 2 else ""
                        if tag == " t" or tag == "\x01t":
                            title = s
                        elif tag == "un":
                            username = s
                        elif not title and tag not in ("un", "ph"):
                            # 第一个非 username/phone 的字符串可能是标题
                            title = s
                except UnicodeDecodeError:
                    pass
            i += max(5, 5 + slen) if 1 <= slen <= 500 else 1
        else:
            i += 1

    return title, username


# ──────────────────────────────────────────────
# 自动发现 Telegram 数据路径
# ──────────────────────────────────────────────

def find_telegram_data() -> Tuple[Path, Path]:
    """
    自动查找 macOS Telegram 的数据库和密钥文件。

    返回: (db_path, key_path)
    """
    container = Path(TELEGRAM_CONTAINER)
    if not container.exists():
        raise FileNotFoundError(
            f"找不到 Telegram 数据目录: {container}\n"
            "请确认已安装 macOS 版 Telegram（App Store 版本）。"
        )

    # 查找 account-* 目录
    account_dirs = sorted(container.glob("account-*"))
    if not account_dirs:
        raise FileNotFoundError("找不到 Telegram 账户目录，请先登录 Telegram。")

    # 使用第一个账户
    account_dir = account_dirs[0]
    db_path = account_dir / "postbox" / "db" / "db_sqlite"
    key_path = container / ".tempkeyEncrypted"

    if not db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")
    if not key_path.exists():
        raise FileNotFoundError(f"密钥文件不存在: {key_path}")

    return db_path, key_path


# ──────────────────────────────────────────────
# 公开 API
# ──────────────────────────────────────────────

def warmup_cache(password: str = DEFAULT_PASSWORD) -> None:
    """预热数据库缓存（供 web 服务启动时调用）。"""
    _db_cache.warmup(password)


def refresh_cache(password: str = DEFAULT_PASSWORD) -> None:
    """强制刷新缓存（重新从加密库导出最新数据）。"""
    _db_cache.refresh(password)


def list_chats(
    password: str = DEFAULT_PASSWORD,
    min_messages: int = 10,
) -> List[ChatInfo]:
    """
    列出本地 Telegram 中的所有群组/频道。

    Args:
        password: 本地密码（未设置则用默认值）
        min_messages: 最少消息数过滤

    Returns:
        按消息数降序排列的 ChatInfo 列表
    """
    import time

    # 命中内存缓存 → 直接返回（跳过 GROUP BY 全表扫描）
    cached = _db_cache.get_chat_list_cache()
    if cached is not None:
        logger.info("[性能] 命中内存缓存，跳过 GROUP BY 查询，返回 %d 个群组", len(cached))
        # 按 min_messages 过滤（缓存存的是 min_messages=1 的完整结果）
        return [c for c in cached if c.message_count >= min_messages]

    t_start = time.time()

    con = _db_cache.get_connection(password)
    t_conn = time.time()
    logger.info("[性能] 获取数据库连接: %.2f 秒", t_conn - t_start)

    # 统计每个 peer 的消息数（1.6GB 表全表扫描，瓶颈所在）
    cur = con.execute(
        "SELECT hex(substr(key,1,8)), count(*) FROM t7 "
        "GROUP BY hex(substr(key,1,8)) HAVING count(*) >= 1 "
        "ORDER BY count(*) DESC",
    )
    peer_stats = cur.fetchall()
    t_query = time.time()
    logger.info("[性能] GROUP BY 查询: %.2f 秒, 返回 %d 个 peer", t_query - t_conn, len(peer_stats))

    # 构建 peer_int64 → (peer_type, peer_id, count) 映射
    chats: List[ChatInfo] = []
    for peer_hex, count in peer_stats:
        peer_raw = struct.unpack(">q", binascii.unhexlify(peer_hex))[0]
        peer_type = (peer_raw >> 32) & 0xFFFFFFFF
        peer_id = peer_raw & 0xFFFFFFFF

        # 只列出群组和频道
        if peer_type not in (PEER_TYPE_GROUP, PEER_TYPE_CHANNEL):
            continue

        # 查找 peer 名称 (t2 key = peer_int64)
        t2_key = (peer_type << 32) | peer_id
        row = con.execute(
            "SELECT value FROM t2 WHERE key = ?", (t2_key,)
        ).fetchone()

        title, username = "", ""
        if row:
            title, username = _parse_peer_title(row[0])

        if not title:
            title = f"Chat_{peer_id}"

        chats.append(ChatInfo(
            peer_id=peer_id,
            peer_type=peer_type,
            title=title,
            username=username,
            message_count=count,
        ))

    t_end = time.time()
    logger.info("[性能] 解析 peer 名称: %.2f 秒, 最终 %d 个群组", t_end - t_query, len(chats))
    logger.info("[性能] list_chats 总耗时: %.2f 秒", t_end - t_start)

    # 保存到内存缓存（后续请求直接命中）
    _db_cache.set_chat_list_cache(chats)

    return [c for c in chats if c.message_count >= min_messages]


def _load_thread_map(
    con: sqlite3.Connection,
    peer_prefix: bytes,
) -> Dict[int, int]:
    """
    从 t62 表构建 msg_id → thread_id 映射。

    t62 是 Telegram macOS 的话题索引表，key 结构 (28 字节):
      peer_int64(8) + namespace(4) + thread_id(4) + padding(4) + timestamp(4) + msg_id(4)

    返回: {msg_id: thread_id}
    """
    cur = con.execute(
        "SELECT key FROM t62 WHERE substr(key,1,8) = ?",
        (peer_prefix,),
    )
    thread_map: Dict[int, int] = {}
    for (key,) in cur:
        if len(key) >= 28:
            # key[12:16] = thread_id (大端 4 字节)
            thread_id = struct.unpack(">I", key[12:16])[0]
            # key[24:28] = msg_id (大端 4 字节)
            msg_id = struct.unpack(">I", key[24:28])[0]
            thread_map[msg_id] = thread_id
    return thread_map


def _load_topic_names(
    con: sqlite3.Connection,
    peer_prefix: bytes,
    thread_ids: set,
) -> Dict[int, str]:
    """
    从话题创建消息的 action 部分解析 Forum 话题名称。

    Telegram Forum 话题的创建消息中包含序列化的 action 数据，
    其中 'title' 字段存储话题名称，格式：title\\x04 + 4字节长度(LE) + UTF-8。

    注意：t62 中的 thread_id 包含两种类型：
    - Forum 话题（有 title 字段）→ 保留
    - 回复线程（无 title 字段）→ 跳过

    只返回能解析出名称的真正 Forum 话题，过滤掉回复线程。
    """
    topics: Dict[int, str] = {}
    title_marker = b"title\x04"

    # thread_id=1 是 Telegram 默认的 General 话题
    if 1 in thread_ids:
        topics[1] = "General"

    # 批量查询：一次性读取该 peer 所有消息中包含 title 标记的行
    # Forum 话题通常不超过几十个，比逐个查询快得多
    cur = con.execute(
        "SELECT key, value FROM t7 WHERE substr(key,1,8) = ? AND instr(value, ?) > 0",
        (peer_prefix, title_marker),
    )
    for key, value in cur:
        # 提取 msg_id
        msg_id = struct.unpack(">I", key[16:20])[0]
        # 只处理属于已知 thread_id 集合的消息
        if msg_id not in thread_ids:
            continue

        # 解析 title 字段
        idx = value.find(title_marker)
        if idx >= 0:
            offset = idx + len(title_marker)
            if offset + 4 <= len(value):
                slen = struct.unpack_from("<I", value, offset)[0]
                if 1 <= slen <= 500 and offset + 4 + slen <= len(value):
                    try:
                        name = value[offset + 4: offset + 4 + slen].decode("utf-8")
                        topics[msg_id] = name
                    except UnicodeDecodeError:
                        pass

    return topics


def load_messages_from_local(
    peer_id: int,
    peer_type: int = PEER_TYPE_CHANNEL,
    password: str = DEFAULT_PASSWORD,
) -> Tuple[str, List[TelegramMessage], Dict[int, str]]:
    """
    从本地 Telegram 数据库读取指定群组的消息。

    Args:
        peer_id: 群组/频道 ID
        peer_type: peer 类型 (2=Channel, 1=Group)
        password: 本地密码

    Returns:
        (chat_name, messages, topics) 元组
        topics: {topic_id: topic_name} 话题名称映射（空 dict 表示非 Forum 群组）
    """
    con = _db_cache.get_connection(password)

    # 获取群名
    t2_key = (peer_type << 32) | peer_id
    row = con.execute(
        "SELECT value FROM t2 WHERE key = ?", (t2_key,)
    ).fetchone()
    chat_name = "Unknown Chat"
    if row:
        title, _ = _parse_peer_title(row[0])
        if title:
            chat_name = title

    # 构建 peer 前缀用于范围查询 (大端序 8 字节)
    peer_int64 = (peer_type << 32) | peer_id
    peer_prefix = struct.pack(">q", peer_int64)

    # 从 t62 表加载 msg_id → thread_id 映射（包含 Forum 话题和回复线程）
    thread_map = _load_thread_map(con, peer_prefix)

    # 提取所有 thread_id，解析 Forum 话题名称（同时过滤掉回复线程）
    all_thread_ids = set(thread_map.values())
    topics = _load_topic_names(con, peer_prefix, all_thread_ids)
    # 少于 2 个话题 → 不是 Forum 群组，清空话题信息
    if len(topics) < 2:
        topics = {}
    # valid_topic_ids 只包含真正的 Forum 话题，回复线程被排除
    valid_topic_ids = set(topics.keys())

    # 查询该 peer 的所有消息
    cur = con.execute(
        "SELECT key, value FROM t7 WHERE substr(key,1,8) = ? ORDER BY key",
        (peer_prefix,),
    )

    # 构建用户名缓存
    user_cache: Dict[int, str] = {}

    messages: List[TelegramMessage] = []
    for key, value in cur:
        pt, pid, msg_id, dt = _parse_message_key(key)
        if dt is None:
            continue

        author_id, text, _ = _parse_message_value(value)
        if not text.strip():
            continue

        # 从 t62 映射获取 thread_id，只保留真正的 Forum 话题
        raw_thread_id = thread_map.get(msg_id)
        thread_id = raw_thread_id if raw_thread_id in valid_topic_ids else None

        # 查找用户名（懒加载缓存）
        author_name = "Unknown"
        if author_id is not None:
            if author_id not in user_cache:
                # author_id 可能是 peer_int64 编码: (type << 32) | actual_id
                actual_id = author_id & 0xFFFFFFFF
                candidates = [author_id, actual_id]
                found = False
                for cand in candidates:
                    user_row = con.execute(
                        "SELECT value FROM t2 WHERE key = ?", (cand,)
                    ).fetchone()
                    if user_row:
                        fn, un = _parse_peer_title(user_row[0])
                        user_cache[author_id] = fn or un or str(actual_id)
                        found = True
                        break
                if not found:
                    user_cache[author_id] = str(actual_id)
            author_name = user_cache[author_id]

        messages.append(TelegramMessage(
            message_id=msg_id,
            date=dt,
            author=author_name,
            text=text,
            reply_to_message_id=None,
            topic_id=thread_id,
        ))

    messages.sort(key=lambda m: (m.date, m.message_id))

    return chat_name, messages, topics


