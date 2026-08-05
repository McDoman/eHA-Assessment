"""Export every generated DOCX and PPTX to PDF using the installed Office apps.

COM automation is used rather than a PDF library so the exported PDF is
rendered by the same engine that renders the source file. Fonts, table banding
and the theme colours therefore match exactly, instead of being approximated by
a second layout engine.
"""

from __future__ import annotations

import glob
import os
import sys

import pythoncom
import win32com.client as win32

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_outroot():
    """Locate the outputs tree whether this sits in build/ or beside outputs/."""
    for cand in (os.path.join(HERE, "outputs"),
                 os.path.join(HERE, "..", "outputs"),
                 os.path.join(os.getcwd(), "outputs")):
        if os.path.isdir(cand):
            return os.path.normpath(cand)
    return os.path.normpath(os.path.join(HERE, ".."))


OUTROOT = _find_outroot()

WD_FORMAT_PDF = 17
PP_FORMAT_PDF = 32


def _abs(p):
    return os.path.abspath(p)


def docx_to_pdf(paths):
    if not paths:
        return []
    pythoncom.CoInitialize()
    word = win32.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    done = []
    try:
        for src in paths:
            dst = os.path.splitext(src)[0] + ".pdf"
            doc = word.Documents.Open(_abs(src), ReadOnly=1)
            try:
                doc.SaveAs(_abs(dst), FileFormat=WD_FORMAT_PDF)
                done.append(dst)
            finally:
                doc.Close(0)
    finally:
        word.Quit()
        pythoncom.CoUninitialize()
    return done


def pptx_to_pdf(paths):
    if not paths:
        return []
    pythoncom.CoInitialize()
    ppt = win32.DispatchEx("PowerPoint.Application")
    done = []
    try:
        for src in paths:
            dst = os.path.splitext(src)[0] + ".pdf"
            pres = ppt.Presentations.Open(_abs(src), ReadOnly=1,
                                          WithWindow=False)
            try:
                pres.SaveAs(_abs(dst), PP_FORMAT_PDF)
                done.append(dst)
            finally:
                pres.Close()
    finally:
        ppt.Quit()
        pythoncom.CoUninitialize()
    return done


def main(patterns=None):
    if patterns:
        docs = [p for p in patterns if p.lower().endswith(".docx")]
        decks = [p for p in patterns if p.lower().endswith(".pptx")]
    else:
        docs = sorted(glob.glob(os.path.join(OUTROOT, "**", "*.docx"),
                                recursive=True))
        decks = sorted(glob.glob(os.path.join(OUTROOT, "**", "*.pptx"),
                                 recursive=True))
    docs = [d for d in docs if not os.path.basename(d).startswith("~$")]
    decks = [d for d in decks if not os.path.basename(d).startswith("~$")]

    made = []
    if docs:
        print(f"Word:       {len(docs)} file(s)")
        made += docx_to_pdf(docs)
    if decks:
        print(f"PowerPoint: {len(decks)} file(s)")
        made += pptx_to_pdf(decks)

    for m in made:
        size = os.path.getsize(m) / 1024
        print(f"  {os.path.relpath(m, OUTROOT)}  ({size:,.0f} KB)")
    return made


if __name__ == "__main__":
    main(sys.argv[1:] or None)
