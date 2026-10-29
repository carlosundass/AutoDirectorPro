# -*- coding: utf-8 -*-
"""
Ayudante de publicación. Lo usa build_exe.bat, no hace falta abrirlo.

  python prepare_release.py
      Lee APP_VERSION de app.py, actualiza version_info.txt y escribe la versión.

  python prepare_release.py --manifest installer\\Instalador-AutoDirectorPro.exe
      Crea installer\\version.json (versión, novedades y huella SHA-256 del
      instalador). Las novedades salen del archivo novedades.txt, si existe.
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def app_version():
    src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        sys.exit("No encontré APP_VERSION en app.py")
    return m.group(1)


def four_parts(version):
    nums = [int(re.sub(r"\D", "", p) or 0) for p in version.split(".")][:4]
    while len(nums) < 4:
        nums.append(0)
    return nums


VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({tuple}),
    prodvers=({tuple}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Hablemos Entonces Producciones'),
        StringStruct('FileDescription', 'AutoDirector Pro | Hablemos Entonces Producciones'),
        StringStruct('FileVersion', '{dotted}'),
        StringStruct('InternalName', 'AutoDirectorPro'),
        StringStruct('LegalCopyright', 'Hablemos Entonces Producciones'),
        StringStruct('OriginalFilename', 'AutoDirectorPro.exe'),
        StringStruct('ProductName', 'AutoDirector Pro | Hablemos Entonces Producciones'),
        StringStruct('ProductVersion', '{dotted}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main():
    version = app_version()
    if len(sys.argv) >= 3 and sys.argv[1] == "--manifest":
        installer = sys.argv[2]
        digest = hashlib.sha256(open(installer, "rb").read()).hexdigest()
        notes_file = os.path.join(HERE, "novedades.txt")
        notes = open(notes_file, encoding="utf-8").read().strip() if os.path.exists(notes_file) else ""
        notes_en_file = os.path.join(HERE, "novedades_en.txt")
        notes_en = open(notes_en_file, encoding="utf-8").read().strip() if os.path.exists(notes_en_file) else ""
        out = os.path.join(os.path.dirname(os.path.abspath(installer)), "version.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"version": version, "notes": notes, "notes_en": notes_en, "sha256": digest}, fh,
                      ensure_ascii=False, indent=2)
        print(out)
        return

    parts = four_parts(version)
    with open(os.path.join(HERE, "version_info.txt"), "w", encoding="utf-8") as fh:
        fh.write(VERSION_TEMPLATE.format(tuple=", ".join(map(str, parts)),
                                         dotted=".".join(map(str, parts))))
    print(version)


if __name__ == "__main__":
    main()
