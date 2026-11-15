"""Read-only ZIP inspection and bounded streaming CRC verification."""
from dataclasses import dataclass, field
import os
import threading
import zipfile
import binascii
import struct

CHUNK = 1024 * 1024
DEFAULT_LIMIT = 4 * 1024**3


class ArchiveChanged(RuntimeError):
    pass


class _Cancelled(Exception):
    pass


def decoded_chunks(handle, info, stop):
    """Decode the compressed span itself; ZipExtFile truncates to file_size."""
    handle.seek(info.header_offset)
    header = handle.read(30)
    if len(header) != 30 or header[:4] != b'PK\x03\x04':
        raise zipfile.BadZipFile('Повреждён локальный заголовок записи.')
    if struct.unpack_from('<H', header, 8)[0] != info.compress_type:
        raise zipfile.BadZipFile('Метод сжатия в заголовках не совпадает.')
    name_size, extra_size = struct.unpack_from('<HH', header, 26)
    handle.seek(info.header_offset + 30 + name_size + extra_size)
    remaining = info.compress_size
    method = info.compress_type
    decoder = None
    if method == zipfile.ZIP_DEFLATED:
        decoder = zipfile.zlib.decompressobj(-15)
    elif method == zipfile.ZIP_BZIP2:
        decoder = zipfile.bz2.BZ2Decompressor()
    elif method == zipfile.ZIP_LZMA:
        if remaining < 9:
            raise zipfile.BadZipFile('Обрезан заголовок LZMA.')
        prefix = handle.read(9)
        if len(prefix) != 9 or struct.unpack_from('<H', prefix, 2)[0] != 5:
            raise zipfile.BadZipFile('Некорректные свойства LZMA.')
        prop = prefix[4]
        if prop >= 9*5*5:
            raise zipfile.BadZipFile('Некорректные свойства LZMA.')
        lc, rest = prop % 9, prop // 9
        lp, pb = rest % 5, rest // 5
        dictionary = struct.unpack_from('<I', prefix, 5)[0]
        if dictionary > 64*1024**2:
            raise NotImplementedError('Словарь LZMA больше 64 МиБ не поддерживается.')
        decoder = zipfile.lzma.LZMADecompressor(format=zipfile.lzma.FORMAT_RAW, filters=[
            {'id':zipfile.lzma.FILTER_LZMA1, 'dict_size':max(dictionary,4096), 'lc':lc, 'lp':lp, 'pb':pb}])
        remaining -= 9
    elif method == getattr(zipfile, 'ZIP_ZSTANDARD', -1):
        decoder = zipfile.zstd.ZstdDecompressor()
    elif method != zipfile.ZIP_STORED:
        raise NotImplementedError('Метод сжатия не поддержан.')

    produced = 0
    while remaining:
        if stop.is_set():
            raise _Cancelled()
        compressed = handle.read(min(64*1024, remaining))
        if not compressed:
            raise zipfile.BadZipFile('Обрезаны сжатые данные.')
        remaining -= len(compressed)
        if decoder is None:
            yield compressed
            continue
        pending = compressed
        while True:
            if stop.is_set():
                raise _Cancelled()
            # One extra output byte detects understated sizes without unbounded expansion.
            maximum = min(CHUNK, max(1, info.file_size-produced+1))
            output = decoder.decompress(pending, maximum)
            if output:
                produced += len(output)
                yield output
            if decoder.eof:
                if decoder.unused_data or remaining:
                    raise zipfile.BadZipFile('Лишние данные после конца сжатого потока.')
                return
            if method == zipfile.ZIP_DEFLATED:
                pending = decoder.unconsumed_tail
                if pending or len(output) == maximum:
                    continue
                break
            pending = b''
            if decoder.needs_input:
                break
    if decoder is not None and not decoder.eof:
        # ZIP LZMA may explicitly omit its EOS marker (general-purpose bit 1).
        if method != zipfile.ZIP_LZMA or info.flag_bits & 2:
            raise zipfile.BadZipFile('Сжатый поток оборван: нет завершающего маркера.')


def signature(handle):
    return stat_signature(os.fstat(handle.fileno()))


def stat_signature(meta):
    return meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns


def ensure_unchanged(handle, path, expected):
    try:
        unchanged = (signature(handle) == expected
                     and stat_signature(os.stat(path)) == expected)
    except OSError as exc:
        raise ArchiveChanged('Архив перемещён или недоступен. Откройте его заново.') from exc
    if not unchanged:
        raise ArchiveChanged('Архив изменился. Откройте его заново.')


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
        ensure_unchanged(handle, path, before)
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
        ensure_unchanged(handle, archive.path, archive.signature)
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
                if member.method == zipfile.ZIP_STORED and member.size != member.packed:
                    result.statuses[index] = 'error'
                    result.errors[index] = 'Размер записи без сжатия не совпадает с размером данных в ZIP.'
                    continue
                # zipfile exposes optional codec modules as None when unavailable.
                codecs = {zipfile.ZIP_DEFLATED: 'zlib', zipfile.ZIP_BZIP2: 'bz2',
                          zipfile.ZIP_LZMA: 'lzma'}
                if hasattr(zipfile, 'ZIP_ZSTANDARD'):
                    codecs[zipfile.ZIP_ZSTANDARD] = 'zstd'
                if member.method != zipfile.ZIP_STORED and member.method not in codecs:
                    result.statuses[index] = 'unsupported'
                    continue
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
                member_bytes = 0
                crc = 0
                try:
                    # ZipInfo is essential: archives can contain duplicate names.
                    # Let zipfile validate filenames and overlapping spans, but do not
                    # use its declared-size-limited reader for integrity validation.
                    with source.open(info):
                        pass
                    for data in decoded_chunks(handle, info, stop):
                        member_bytes += len(data)
                        if member_bytes > member.size:
                            raise zipfile.BadZipFile('Распакованных данных больше заявленного размера записи.')
                        result.read_bytes += len(data)
                        crc = binascii.crc32(data, crc)
                        if progress:
                            progress(index, result.read_bytes)
                    if stop.is_set():
                        result.cancelled = True
                        break
                    if member_bytes != member.size:
                        raise zipfile.BadZipFile(
                            f'Размер записи не совпадает: заявлено {member.size}, прочитано {member_bytes}.')
                    if crc & 0xffffffff != info.CRC:
                        raise zipfile.BadZipFile('Контрольная сумма CRC не совпадает.')
                    result.statuses[index] = 'ok'
                except _Cancelled:
                    result.cancelled = True
                    break
                except NotImplementedError:
                    result.statuses[index] = 'unsupported'
                except Exception as exc:
                    budget_used += max(0, member_bytes-member.size)
                    result.statuses[index] = 'error'
                    result.errors[index] = str(exc)
            ensure_unchanged(handle, archive.path, archive.signature)
    result.complete = (not result.cancelled and not result.limited
                       and len(result.statuses) == len(archive.members)
                       and all(status == 'ok' for status in result.statuses.values()))
    return result


def format_size(size):
    for unit in ('Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'):
        if abs(size) < 1024 or unit == 'ТиБ':
            return f'{size:.1f} {unit}' if unit != 'Б' else f'{size} Б'
        size /= 1024
