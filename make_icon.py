# -*- coding: utf-8 -*-
"""
Crea a partir del logo de Hablemos Entonces todo lo gráfico del programa:
  - logo.png              : logo recortado (cabecera del programa)
  - icon.ico              : icono del .exe, de la ventana y del instalador
  - wizard_large*.bmp     : imagen lateral del instalador
  - wizard_small*.bmp     : imagen pequeña (arriba a la derecha) del instalador

Usa logo_original.png (la misma carpeta). Si no existe, lo descarga de internet.
Uso:  python make_icon.py            (o:  python make_icon.py otro_logo.png)
"""
import io
import os
import sys
import urllib.request

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://i.postimg.cc/G3Vzpf2S/Hablemos-entonces.png"
LAVENDER = (244, 241, 248)      # mismo fondo pastel de la app
BAND = (207, 202, 248)          # lila del botón principal
WHITE = (255, 255, 255)


def out(name):
    return os.path.join(HERE, name)


def load_logo():
    if len(sys.argv) > 1:
        return Image.open(sys.argv[1])
    local = out("logo_original.png")
    if os.path.exists(local):
        return Image.open(local)
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return Image.open(io.BytesIO(resp.read()))


def fit(img, box_w, box_h):
    """Escala el logo para que quepa en la caja sin deformarlo."""
    k = min(box_w / img.width, box_h / img.height)
    return img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)


def wizard_large(logo, w, h):
    """Imagen lateral del instalador: fondo lavanda, logo arriba, franja lila abajo."""
    canvas = Image.new("RGB", (w, h), LAVENDER)
    band_h = round(h * 0.10)
    canvas.paste(BAND, (0, h - band_h, w, h))
    lg = fit(logo, round(w * 0.88), round(w * 0.88))
    x = (w - lg.width) // 2
    y = round((h - band_h) * 0.47 - lg.height / 2)
    canvas.paste(lg, (x, y), lg)
    return canvas


def wizard_small(logo, w, h):
    """Imagen pequeña del instalador: logo sobre blanco."""
    canvas = Image.new("RGB", (w, h), WHITE)
    lg = fit(logo, round(w * 0.92), round(h * 0.92))
    canvas.paste(lg, ((w - lg.width) // 2, (h - lg.height) // 2), lg)
    return canvas


def main():
    img = load_logo().convert("RGBA")

    # Quita el espacio transparente sobrante alrededor del dibujo
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    img.save(out("logo.png"))

    # Icono: cuadrado, con un pequeño margen, en todos los tamaños de Windows
    side = int(max(img.size) * 1.06)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    sq = sq.resize((256, 256), Image.LANCZOS)
    sq.save(out("icon.ico"), sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])

    # Instalador (Inno Setup elige el tamaño que mejor se ajusta a la pantalla)
    for name, (w, h) in {"wizard_large.bmp": (164, 314), "wizard_large_144.bmp": (246, 459),
                         "wizard_large_192.bmp": (328, 628)}.items():
        wizard_large(img, w, h).save(out(name))
    for name, (w, h) in {"wizard_small.bmp": (55, 55), "wizard_small_144.bmp": (83, 80),
                         "wizard_small_192.bmp": (110, 106)}.items():
        wizard_small(img, w, h).save(out(name))
    print("Listo: logo.png, icon.ico y las imágenes del instalador creados.")


if __name__ == "__main__":
    main()
