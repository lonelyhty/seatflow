"""Validate and import locally obtained upstream scripts; never download or execute them here."""
from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from pathlib import Path

from .credentials import data_dir

# Hashes of the browser-delivered versions used during the original validation.
# Public availability does not license redistribution; these files are not shipped.
ASSET_HASHES = {
    'submitVerify.min.js': '1b0282c64421fae9f018cd341a355c3df215b3f3383ef5f114cf226a3592df7a',
    'md5.js': '3e16fe48572dcb08da6583ba5acecfac5f5b04841dc7c2c34396913fa20094e2',
}


class SigningAssetsError(RuntimeError):
    pass


def asset_directory() -> Path:
    override = os.environ.get('SEATFLOW_SIGNING_DIR')
    return Path(override) if override else data_dir() / 'signing'


def _validated_content(directory: Path) -> dict[str, bytes]:
    contents = {}
    for name, expected in ASSET_HASHES.items():
        try:
            with (directory / name).open('rb') as stream:
                content = stream.read(2_000_001)
        except OSError:
            raise SigningAssetsError(f'缺少本地依赖 {name}；请查看 docs/PROTOCOL.md。') from None
        if len(content) > 2_000_000 or hashlib.sha256(content).hexdigest() != expected:
            raise SigningAssetsError(f'{name} 与已验证的版本不一致，未导入或执行。')
        contents[name] = content
    return contents


def verify_assets(directory: Path | None = None) -> dict[str, str]:
    _validated_content(directory if directory is not None else asset_directory())
    return dict(ASSET_HASHES)


def import_assets(source: Path, destination: Path | None = None) -> Path:
    contents = _validated_content(source)
    destination = destination if destination is not None else asset_directory()
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination, prefix='.asset-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, destination / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='检查或导入协议模式的本地第三方依赖；不会登录或预约')
    parser.add_argument('--from-directory', type=Path, help='含两个原始脚本的本地目录；省略时仅检查当前依赖')
    args = parser.parse_args(argv)
    try:
        if args.from_directory:
            import_assets(args.from_directory)
        verify_assets()
    except (SigningAssetsError, OSError) as error:
        print(str(error) if isinstance(error, SigningAssetsError) else '本地依赖无法写入，请检查目录权限。')
        return 2
    print('协议模式本地依赖校验通过。未发出网络请求。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
