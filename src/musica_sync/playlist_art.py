"""Render consistent genre artwork from vector shapes and typography."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import textwrap
from html import escape
from pathlib import Path

from musica_sync.navidrome import ROOT

PALETTE = ["#6639bb", "#126a66", "#a13358", "#285bc4", "#8d4125", "#4a5c20", "#176385", "#833fa0"]


def artwork(title: str) -> tuple[Path, str]:
    colour = PALETTE[int(hashlib.sha256(title.encode()).hexdigest()[:8], 16) % len(PALETTE)]
    lines = textwrap.wrap(title.upper(), width=14, break_long_words=False)
    labels = "".join(
        f'<text x="48" y="{280 + index * 76}" font-size="66" font-weight="800">'
        f"{escape(line)}</text>"
        for index, line in enumerate(lines)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"
      viewBox="0 0 600 600">
      <defs><linearGradient id="bg" x2="1" y2="1">
        <stop stop-color="{colour}"/><stop offset="1" stop-color="#10101a"/>
      </linearGradient></defs>
      <rect width="600" height="600" fill="url(#bg)"/>
      <g fill="none" stroke="#fff" opacity=".09" stroke-width="32">
        <circle cx="570" cy="35" r="120"/><circle cx="570" cy="35" r="190"/>
      </g>
      <g fill="#fff" font-family="sans-serif">
        <text x="48" y="82" font-size="20" letter-spacing="3" opacity=".8">LA TUA LIBRERIA</text>
        {labels}
        <rect x="48" y="484" width="46" height="5" rx="2"/>
        <text x="48" y="548" font-size="24" font-weight="600" opacity=".8">Musica</text>
      </g>
    </svg>'''
    digest = hashlib.sha256(svg.encode()).hexdigest()
    directory = ROOT / "assets/playlist-covers"
    path = directory / f"{digest}.png"
    if not path.exists():
        if not shutil.which("rsvg-convert"):
            raise RuntimeError("Install rsvg-convert (librsvg) to render playlist artwork.")
        directory.mkdir(parents=True, exist_ok=True)
        rendered = subprocess.run(
            ["rsvg-convert"], input=svg.encode(), capture_output=True, check=True
        )
        path.with_suffix(".svg").write_text(svg)
        path.write_bytes(rendered.stdout)
    return path, digest
