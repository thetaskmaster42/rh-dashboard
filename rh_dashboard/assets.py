"""
Binary assets embedded in the page.

The dashboard's guarantee is that the generated HTML opens offline with no
external request of any kind — no script, no stylesheet, no font, and no
favicon fetch either. So the icon lives here as base64 rather than as a file
the page links to, and the browser tab works from a copy mailed to someone
with no network at all.

Regenerating from `images/icon.png` (the 512x512 original):

    python3 - <<'EOF'
    from PIL import Image
    import base64, io
    im = Image.open("images/icon.png").convert("RGBA").resize((32, 32), Image.LANCZOS)
    im = im.quantize(colors=64, method=Image.FASTOCTREE)
    buf = io.BytesIO(); im.save(buf, format="PNG", optimize=True)
    print(base64.b64encode(buf.getvalue()).decode())
    EOF

Pillow is only needed for that one-off step and is deliberately not a
dependency of anything that runs — the committed bytes below are what ship.
32x32, quantised to 64 colours: 880 bytes, 1176 as base64. The
full-colour version was 2465 bytes for no visible gain at tab size.
"""

# 32x32 PNG, base64. Wrapped for readability; joined on import.
FAVICON_PNG_B64 = "".join((
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAMAAABEpIrGAAAA/1BMVEUAAADoumr928z3xnHlrEv+"
    "5tfwtVIvJxYBAAAYFRHtxrkAAAAAAAADAwIAAABGeJwjHBBpVCsAAAAMCgYtKSbFmU5oWFSph0oj"
    "PlJHNxrTr6QOIjGrk4vMpF1XSSeMdm6MajGfhXu2hzu6lVT/2HYcMkOedjTXrWLcsWT/w1bzzsEj"
    "HxQ7OwopRlxIOShTRkNHfKRnYAuRdEL96YL///9Jgar//+06bJBfPx9VVVWqVVXJlT/Un0YAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAB1I3e3AAAAQHRSTlMA/v7+/v79/tH9/ousMnH9/f5J9f7+///+/v/+/v7+//7+"
    "/v/9/////vz+FAX+///7Ff/9Afr9/wgDA//cAAAAkNB4agAAAeBJREFUeNp1k+mWoyAQhVFEQIWw"
    "uLTGrGZPZ510z/r+zzWAJic9ca7+4dTHrSuUADwrSYh2LwN9ugF6LdbTebYqOegXGS28MAy3uMeB"
    "c8b2qoS2vkp6NrOEEBITOc/mU83+04ERLDHGY8x+9pU5Scj428I8MgaX17r5NqVTgVMsShz3GMRA"
    "0Qaum6zIwkBewPsLoegIeh4cwTAIt39eT4LpzNY9UcAgCMKXs/pBCrcfayoNEQZC/RNhbOoePMqy"
    "aLzQEHPyNYXeOAAXpe302oRLW/dgQbVtZS2yZ4sLyRxgQggqO4unK7sB4XWCuMHGw1rItgdTigF1"
    "hF0dYk/KFmgvVdHhUO+ZXLTAdISvNGuBuQPiw/m8TBI8gs5jLDS9dilXyc0Cb4PBMK9zbD7PaiMa"
    "S1pg6q7MAfUn8idiXGRrD66dkzsI1gLn7wPf9xHa5Wlqx0VuXIRtZqY7aR0OFXII8qOqrnOxMZMb"
    "TEU6i3LugHRiAb+F0GcuF7+lONU7hDpgOLsDfhRF/kxIkVfGrzp2LR4AmqRGOJ2Yzf5uojkAPQ7G"
    "wy6rtP07HPDI4EKYBYpy0g1MfPj4WOpTHaEHZAxqEe/vg5Aul1SpxMSqolZVdXr+vRXn1oxxQu8i"
    "/Nej/BejQSc/5zjDwQAAAABJRU5ErkJggg==",
))

FAVICON_DATA_URI = f"data:image/png;base64,{FAVICON_PNG_B64}"

__all__ = ["FAVICON_PNG_B64", "FAVICON_DATA_URI"]
