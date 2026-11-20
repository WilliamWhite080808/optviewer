"""Rebuild with the original bundled Python/matplotlib; no downloads required.

The 1.0 optimization renderer was supplied only as an executable. Preserve its
Python 3.14 bytecode, runtime and bootloader while replacing the CLI entry point.
"""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parent
MAGIC = b'MEI\x0c\x0b\x0a\x0b\x0e'


def unpack(executable):
    data = executable.read_bytes()
    pos = data.rfind(MAGIC)
    if pos < 0:
        raise ValueError('Not a PyInstaller executable')
    _, length, toc_pos, toc_size, version, library = struct.unpack('!8sIIII64s', data[pos:pos + 88])
    start = pos + 88 - length
    toc = data[start + toc_pos:start + toc_pos + toc_size]
    offset, entries = 0, []
    while offset < len(toc):
        size, loc, packed, raw, compressed, kind = struct.unpack('!IIIIBc', toc[offset:offset + 18])
        name = toc[offset + 18:offset + size].rstrip(b'\0').decode()
        offset += size
        entries.append((name, kind, compressed, raw, data[start + loc:start + loc + packed]))
    return data[:start], entries, version, library


def main():
    os.chdir(ROOT)
    exe = ROOT / 'optviewer.exe'
    backup = ROOT / 'backups' / 'optviewer-1.0.0.exe'
    backup.parent.mkdir(exist_ok=True)
    if not backup.exists():
        shutil.copy2(exe, backup)
    prefix, entries, version, library = unpack(backup)
    if version != 314:
        raise ValueError('Expected original Python 3.14 bundle')
    build = ROOT / '.build'
    runtime = build / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    legacy = None
    for name, kind, compressed, raw, payload in entries:
        content = zlib.decompress(payload) if compressed else payload
        if name == 'optviewer' and kind == b's':
            legacy = content
        if kind in (b'b', b'x', b'z'):
            target = (runtime / name).resolve()
            if not target.is_relative_to(runtime.resolve()):
                raise ValueError('Archive path escapes build directory')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    if legacy is None:
        raise ValueError('Original renderer entry not found')
    (ROOT / 'legacy_optviewer.marshal').write_bytes(legacy)
    compiler = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler), '/nologo', f'/out:{build / "PythonHost.exe"}', str(ROOT / 'tools/PythonHost.cs')], check=True)
    compile_script = build / 'compile_entry.py'
    compile_script.write_text("import marshal\nsource = open('optviewer.py', 'r', encoding='utf-8').read()\nopen('.build/new_main.marshal', 'wb').write(marshal.dumps(compile(source, 'optviewer.py', 'exec')))\n", encoding='utf-8')
    subprocess.run([str(build / 'PythonHost.exe'), str(compile_script)], check=True)
    new_code = (build / 'new_main.marshal').read_bytes()
    updated = []
    for entry in entries:
        name, kind, compressed, raw, payload = entry
        if name == 'optviewer' and kind == b's':
            entry = (name, kind, 1, len(new_code), zlib.compress(new_code, 9))
        updated.append(entry)
    updated.append(('legacy_optviewer.marshal', b'x', 1, len(legacy), zlib.compress(legacy, 9)))
    body, table = bytearray(), bytearray()
    for name, kind, compressed, raw, payload in updated:
        encoded = name.encode() + b'\0'
        size = (18 + len(encoded) + 15) // 16 * 16
        table += struct.pack('!IIIIBc', size, len(body), len(payload), raw, compressed, kind)
        table += encoded + b'\0' * (size - 18 - len(encoded))
        body += payload
    cookie = struct.pack('!8sIIII64s', MAGIC, len(body) + len(table) + 88, len(body), len(table), version, library)
    staged = build / 'optviewer.exe'
    staged.write_bytes(prefix + body + table + cookie)
    # Original executable has the Windows read-only attribute.
    exe.chmod(0o666)
    shutil.copyfile(staged, exe)
    print('Built optviewer.exe; original preserved in backups/optviewer-1.0.0.exe')


if __name__ == '__main__':
    main()
