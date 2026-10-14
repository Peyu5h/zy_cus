import base64
import io
import re

import pypdfium2 as pdfium


def pages(path, dpi=150):
    """Each page as a PNG data URL, plus its text layer when the PDF has a real one."""
    out = []
    for page in pdfium.PdfDocument(path):
        buf = io.BytesIO()
        page.render(scale=dpi / 72, grayscale=True).to_pil().save(buf, "PNG", optimize=True)
        text = page.get_textpage().get_text_range().strip()
        out.append({"image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                    "text": text if len(re.findall(r"\w", text)) > 40 else ""})
    return out
