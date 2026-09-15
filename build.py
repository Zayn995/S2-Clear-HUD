"""Build independently generated HUD textures from local game package metadata."""
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile

from PIL import Image

VERSION = '0.1.0'
BASE = 'zzz_S2ClearHUD_P'
ACTIVE = 'T_itemselector_additional_bg_active'
ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def texture_info(raw):
    formats = list(re.finditer(rb'PF_[A-Z0-9_]+\0', raw))
    require(len(formats) == 1, 'Expected exactly one texture platform format.')
    fmt = formats[0]
    w, h, slices, length = struct.unpack_from('<4I', raw, fmt.start() - 16)
    require(slices == 1 and length == len(fmt.group()), 'Unsupported texture layout.')
    require(0 < w <= 4096 and 0 < h <= 4096, 'Unexpected texture dimensions.')
    return fmt.group()[:-1].decode(), w, h, fmt.end()


def transparent_pixels(fmt, w, h):
    if fmt == 'PF_B8G8R8A8':
        return b'\0' * (w * h * 4)
    blocks = ((w + 3) // 4) * ((h + 3) // 4)
    if fmt == 'PF_BC7':
        return (b'\x40' + b'\0' * 15) * blocks
    if fmt == 'PF_DXT1':
        return (b'\0' * 4 + b'\xff' * 4) * blocks
    raise ValueError(f'Unsupported texture format: {fmt}')


def selection_pixels(width, height):
    """Generate an amber capsule near the lower edge on a clear BGRA canvas."""
    import math
    pixels = bytearray(width * height * 4)
    left, right = width * 0.08, width * 0.92
    center_y = height * 0.95
    radius = max(1.0, height * 0.012)
    for y in range(max(0, int(center_y - radius - 1)), min(height, int(center_y + radius + 2))):
        for x in range(max(0, int(left - radius - 1)), min(width, int(right + radius + 2))):
            nearest_x = min(right, max(left, x + 0.5))
            distance = math.hypot(x + 0.5 - nearest_x, y + 0.5 - center_y)
            alpha = round(255 * min(1.0, max(0.0, radius + 0.5 - distance)))
            if alpha:
                offset = (y * width + x) * 4
                pixels[offset:offset + 4] = bytes((56, 150, 224, alpha))
    return bytes(pixels)


def verify_alpha(fmt, w, h, pixels):
    if fmt == 'PF_B8G8R8A8':
        require(not any(pixels[3::4]), 'Unexpected visible background pixels.')
        return
    fourcc = b'DX10' if fmt == 'PF_BC7' else b'DXT1'
    header = struct.pack('<7I', 124, 0x81007, h, w, len(pixels), 0, 1)
    header += b'\0' * 44 + struct.pack('<II4s5I', 32, 4, fourcc, 0, 0, 0, 0, 0)
    header += struct.pack('<5I', 0x1000, 0, 0, 0, 0)
    if fourcc == b'DX10':
        header += struct.pack('<5I', 98, 3, 0, 1, 0)
    with Image.open(BytesIO(b'DDS ' + header + pixels)) as image:
        require(image.size == (w, h), 'Decoded dimensions do not match.')
        require(image.convert('RGBA').getchannel('A').getextrema() == (0, 0),
                'Compressed background is not fully transparent.')


def build(args):
    retoc = Path(args.retoc).resolve()
    game = Path(args.game_paks).resolve()
    output = Path(args.output).resolve()
    require(retoc.is_file(), 'retoc.exe was not found.')
    require(all((game / f'global.{ext}').is_file() for ext in ('utoc', 'ucas')),
            'The game Paks directory must contain global.utoc and global.ucas.')
    require(not output.is_relative_to(game), 'Choose an output directory outside the game Paks directory.')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'S2-Clear-HUD-v{VERSION}-game-2.0.5.zip'
    require(not archive.exists(), f'Output already exists: {archive}')
    work = Path(tempfile.mkdtemp(prefix='.build-', dir=output))
    print(f'Local working files: {work}')
    counter = 0

    def run(*arguments):
        nonlocal counter
        counter += 1
        result = subprocess.run([str(retoc), *map(str, arguments)], capture_output=True)
        log = work / f'{counter:02d}-{arguments[0]}.log'
        log.write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f'retoc failed; see {log}')

    targets = json.loads((ROOT / 'targets.json').read_text())
    require(len(targets) == len(set(targets)) == 33, 'Unexpected target manifest.')
    relative_paths = [p.removeprefix('../../../') for p in targets]
    require(all(p.startswith('Stalker2/Content/GameLite/FPS_Game/UIRemaster/UITextures/')
                and '..' not in Path(p).parts and p.endswith('.uasset') for p in relative_paths),
            'Invalid target path.')
    current = work / 'current-assets'
    extraction = ['to-legacy', game, current, '--version', 'UE5_5', '--no-shaders', '--no-script-objects']
    for path in relative_paths:
        extraction += ['--filter', path]
    run(*extraction)
    expected_paths = set(relative_paths)
    require({p.relative_to(current).as_posix() for p in current.rglob('*.uasset')} == expected_paths,
            'Current game texture paths do not match the supported version.')

    modified = work / 'modified-assets'
    records = []
    for relative in sorted(expected_paths):
        asset = current / relative
        raw = asset.with_suffix('.uexp').read_bytes()
        fmt, w, h, pos = texture_info(raw)
        require(struct.unpack_from('<3I', raw, pos) == (0, 1, 0),
                f'Unsupported current mip layout: {asset.name}')
        start = pos + 12
        pixels = transparent_pixels(fmt, w, h)
        if asset.stem == ACTIVE:
            require(fmt == 'PF_B8G8R8A8', 'Active-slot format changed.')
            pixels = selection_pixels(w, h)
        else:
            verify_alpha(fmt, w, h, pixels)
        end = start + len(pixels)
        require(struct.unpack_from('<3I', raw, end) == (w, h, 1), 'Pixel payload size mismatch.')
        require(raw[end + 12:] == b'\0' * 12 + b'\xc1\x83\x2a\x9e', 'Unexpected texture trailer.')
        updated = raw[:start] + pixels + raw[end:]
        require(len(updated) == len(raw), 'Export length changed.')
        dest = modified / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, dest)
        dest.with_suffix('.uexp').write_bytes(updated)
        records.append({'path': relative, 'export_sha256': digest(updated),
                        'format': fmt, 'width': w, 'height': h, 'start': start, 'size': len(pixels)})

    package = work / 'package'
    package.mkdir()
    run('to-zen', modified, package / f'{BASE}.utoc', '--version', 'UE5_5')
    run('verify', package / f'{BASE}.utoc')
    check_input = work / 'verify-input'
    check_input.mkdir()
    for ext in ('pak', 'utoc', 'ucas'):
        shutil.copyfile(package / f'{BASE}.{ext}', check_input / f'{BASE}.{ext}')
    for ext in ('utoc', 'ucas'):
        shutil.copyfile(game / f'global.{ext}', check_input / f'global.{ext}')
    readback = work / 'readback'
    run('to-legacy', check_input, readback, '--version', 'UE5_5', '--no-shaders', '--no-script-objects')
    require({p.relative_to(readback).as_posix() for p in readback.rglob('*.uasset')} == expected_paths,
            'Read-back paths differ from the target manifest.')
    for item in records:
        data = (readback / item['path']).with_suffix('.uexp').read_bytes()
        require(digest(data) == item['export_sha256'], f"Read-back export changed: {item['path']}")
        pixels = data[item['start']:item['start'] + item['size']]
        if Path(item['path']).stem == ACTIVE:
            require(pixels == selection_pixels(item['width'], item['height']), 'Generated selection indicator changed.')
        else:
            verify_alpha(item['format'], item['width'], item['height'], pixels)

    shutil.copyfile(ROOT / 'release/INSTALL.txt', package / 'README.txt')
    names = ['README.txt'] + [f'{BASE}.{ext}' for ext in ('pak', 'utoc', 'ucas')]
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in names:
            z.write(package / name, name)
    with zipfile.ZipFile(archive) as z:
        require(set(z.namelist()) == set(names) and z.testzip() is None, 'ZIP integrity check failed.')
        for name in names:
            require(z.read(name) == (package / name).read_bytes(), 'ZIP content changed.')
    summary = {'version': VERSION, 'textures': len(records), 'export_roundtrip_exact': True,
               'procedural_selection_verified': True, 'external_mod_inputs': False, 'game_started': False, 'play_tested': False,
               'archive_sha256': digest(archive.read_bytes()), 'exports': records}
    (output / 'verification.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(f'Built and verified: {archive}')
    print('33 generated textures. Independent selection indicator verified. Not play-tested.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retoc', required=True, help='Path to the separately downloaded retoc.exe')
    parser.add_argument('--game-paks', required=True, help='Installed game Stalker2/Content/Paks directory')
    parser.add_argument('--output', default='build-local', help='Local output directory; never published automatically')
    arguments = parser.parse_args()
    try:
        build(arguments)
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile, struct.error) as error:
        parser.exit(1, f'Build failed: {error}\n')
