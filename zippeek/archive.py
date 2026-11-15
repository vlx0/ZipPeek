"""Read-only ZIP inspection and bounded streaming CRC verification."""
from dataclasses import dataclass, field
import os
import threading
import zipfile

CHUNK = 1024 * 1024
DEFAULT_LIMIT = 4 * 1024**3


class ArchiveChanged(RuntimeError):
    pass


def signature(handle):
    meta = os.fstat(handle.fileno())
    return meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns


@dataclass(frozen=True)
class Member:
    index: int
    name: str
    size: int
    packed: int
    is_dir: bool
    encrypted: bool
    method: int


@dataclass(frozen=True)
class Archive:
    path: str
    signature: tuple
    members: tuple[Member, ...]

    @property
    def size(self):
        return sum(member.size for member in self.members)

    @property
    def packed(self):
        return self.signature[2]


@dataclass
class Check:
    statuses: dict[int, str] = field(default_factory=dict)
    read_bytes: int = 0
    errors: dict[int, str] = field(default_factory=dict)
    complete: bool = False
    cancelled: bool = False
    limited: bool = False


def inspect_archive(path, stop=None):
    if stop is not None and stop.is_set():
        raise InterruptedError('Чтение содержимого остановлено.')
    path = os.path.abspath(path)
    members = []
    with open(path, 'rb') as handle:
        before = signature(handle)
        with zipfile.ZipFile(handle) as archive:
            for index, info in enumerate(archive.infolist()):
                if stop is not None and stop.is_set():
                    raise InterruptedError('Чтение содержимого остановлено.')
                members.append(Member(index, info.filename, info.file_size, info.compress_size,
                                      info.is_dir(), bool(info.flag_bits & 1), info.compress_type))
        if before != signature(handle):
            raise ArchiveChanged('Архив изменился во время чтения. Откройте его заново.')
    return Archive(path, before, tuple(members))


def verify(archive, stop, max_bytes=DEFAULT_LIMIT, progress=None):
    if max_bytes is not None and max_bytes < 0:
        raise ValueError('Лимит не может быть отрицательным.')
    result = Check()
    budget_used = 0
    if stop.is_set():
        result.cancelled = True
        return result
    with open(archive.path, 'rb') as handle:
        if signature(handle) != archive.signature:
            raise ArchiveChanged('Архив изменился. Откройте его заново.')
        with zipfile.ZipFile(handle) as source:
            infos = source.infolist()
            if len(infos) != len(archive.members):
                raise ArchiveChanged('Содержимое архива изменилось. Откройте его заново.')
            for member, info in zip(archive.members, infos):
                index = member.index
                if stop.is_set():
                    result.cancelled = True
                    break
                if member.encrypted:
                    result.statuses[index] = 'encrypted'
                    continue
                # zipfile exposes optional codec modules as None when unavailable.
                codecs = {zipfile.ZIP_DEFLATED: 'zlib', zipfile.ZIP_BZIP2: 'bz2',
                          zipfile.ZIP_LZMA: 'lzma'}
                if hasattr(zipfile, 'ZIP_ZSTANDARD'):
                    codecs[zipfile.ZIP_ZSTANDARD] = 'zstd'
                codec = codecs.get(member.method)
                if codec is not None and getattr(zipfile, codec, None) is None:
                    result.statuses[index] = 'unsupported'
                    continue
                if max_bytes is not None and member.size > max_bytes - budget_used:
                    result.limited = True
                    break
                # Reserve the declared size before reading: a CRC exception can consume
                # the payload without returning any bytes from read().
                budget_used += member.size
                try:
                    # ZipInfo is essential: archives can contain duplicate names.
                    with source.open(info) as stream:
                        while True:
                            if stop.is_set():
                                result.cancelled = True
                                break
                            chunk_size = CHUNK
                            if max_bytes is not None:
                                remaining = max_bytes - result.read_bytes
                                if remaining <= 0:
                                    # Reading the last exact chunk triggers zipfile's CRC check.
                                    if stream.tell() >= member.size:
                                        stream.read(1)  # Also validate CRC for empty members.
                                        break
                                    result.limited = True
                                    break
                                chunk_size = min(chunk_size, remaining)
                            data = stream.read(chunk_size)
                            if not data:
                                break
                            result.read_bytes += len(data)
                            if progress:
                                progress(index, result.read_bytes)
                        if result.cancelled or result.limited:
                            break
                    result.statuses[index] = 'ok'
                except NotImplementedError:
                    result.statuses[index] = 'unsupported'
                except (zipfile.BadZipFile, RuntimeError, EOFError, OSError, ValueError) as exc:
                    result.statuses[index] = 'error'
                    result.errors[index] = str(exc)
                except Exception as exc:
                    # Decompressors may raise zlib.error / lzma.LZMAError / bz2 errors.
                    result.statuses[index] = 'error'
                    result.errors[index] = str(exc)
            if signature(handle) != archive.signature:
                raise ArchiveChanged('Архив изменился во время проверки. Результат недействителен.')
    result.complete = (not result.cancelled and not result.limited
                       and len(result.statuses) == len(archive.members)
                       and all(status == 'ok' for status in result.statuses.values()))
    return result


def format_size(size):
    for unit in ('Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'):
        if abs(size) < 1024 or unit == 'ТиБ':
            return f'{size:.1f} {unit}' if unit != 'Б' else f'{size} Б'
        size /= 1024
