"""Section 3.5 figures, in the style of transports_svg.py.

    cd docs/diagrams && python files_svg.py
"""

o = []
a = o.append


def start(w, h, label):
    o.clear()
    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="0 0 {w} {h}" style="max-width:{w}px;background:#fff" '
      f'font-family="Helvetica, Arial, sans-serif" font-size="16" role="img" aria-label="{label}">')
    a('<defs>'
      '<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
      '<path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker>'
      '<marker id="arf" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
      '<path d="M0,0 L10,5 L0,10 z" fill="#c9a400"/></marker>'
      '<marker id="arx" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
      '<path d="M0,0 L10,5 L0,10 z" fill="#d33"/></marker>'
      '</defs>')


def save(path):
    a('</svg>')
    open(path, 'w').write("\n".join(o))


def rect(x, y, w, h, fill, stroke, dash=False, rx=12):
    a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="2"'
      + (' stroke-dasharray="8 5"' if dash else '') + '/>')


def text(x, y, t, size=16, weight="normal", anchor="middle", fill="#222"):
    for i, line in enumerate(t.split("\n")):
        a(f'<text x="{x}" y="{y + i * (size + 4)}" text-anchor="{anchor}" font-size="{size}" font-weight="{weight}" '
          f'fill="{fill}">{line}</text>')


def tag(x, y, t, size=13, fill="#222"):
    w = len(t) * size * 0.56 + 10
    a(f'<rect x="{x - w / 2:.1f}" y="{y - size - 1}" width="{w:.1f}" height="{size + 7}" fill="#fff"/>')
    text(x, y, t, size, fill=fill)

def line(pts, color="#333", marker="ar", dash=False):
    d = " ".join(f"{x},{y}" for x, y in pts)
    a(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#{marker})"'
      + (' stroke-dasharray="7 5"' if dash else '') + '/>')


def box(x, y, w, h, title, sub=None):
    rect(x, y, w, h, "#fff", "#333")
    if sub:
        text(x + w / 2, y + h / 2 - 4, title, 16, "bold")
        text(x + w / 2, y + h / 2 + 18, sub, 13, fill="#555")
    else:
        text(x + w / 2, y + h / 2 + 6, title, 16, "bold")


def filebox(x, y, w, label, sub):
    rect(x, y, w, 64, "#fffbe0", "#c9a400", rx=8)
    text(x + w / 2, y + 28, label, 15, "bold")
    text(x + w / 2, y + 48, sub, 13, fill="#555")


def machine(x, y, w, h, title, sub):
    rect(x, y, w, h, "#fff", "#333", dash=True)
    text(x + w / 2, y + 28, title, 16, "bold")
    text(x + w / 2, y + 50, sub, 14, fill="#555")


def failure():
    start(1000, 760, "On one machine the path opens. Deployed, the browser cannot reach the file on the server container.")
    rect(20, 20, 960, 260, "#eef5ff", "#5b8def")
    text(40, 52, "On your laptop: one machine. The path works.", 18, "bold", "start")
    box(50, 80, 250, 100, "Host application", "on your computer")
    box(650, 80, 270, 100, "Server process", "python server.py")
    line([(300, 115), (648, 115)])
    text(475, 107, "tools/call", 14)
    line([(650, 150), (302, 150)])
    text(475, 142, '"path": "/tmp/effis/map.png"', 14)
    filebox(650, 200, 270, "map.png", "/tmp/effis/, same computer")
    line([(785, 180), (785, 198)])
    line([(175, 180), (175, 232), (648, 232)], "#c9a400", "arf", dash=True)
    text(475, 224, "you open the path", 14)

    rect(20, 310, 960, 430, "#fff7ec", "#e8913a")
    text(40, 342, "Deployed: three machines. The path fails.", 18, "bold", "start")
    machine(40, 365, 270, 355, "Machine A", "the user's browser")
    machine(365, 365, 270, 355, "Machine B", "EVE backend")
    machine(690, 365, 270, 355, "Machine C", "MCP server container")
    box(75, 435, 200, 80, "Chat")
    box(400, 435, 200, 80, "Agent")
    box(725, 435, 200, 80, "Server", "same code")
    filebox(725, 560, 200, "map.png", "/tmp/effis/, machine C")
    line([(825, 515), (825, 558)])
    line([(725, 460), (602, 460)])
    tag(663, 450, "the path")
    line([(400, 490), (277, 490)])
    tag(338, 480, "the path")
    line([(175, 515), (175, 580), (723, 580)], "#d33", "arx", dash=True)
    tag(338, 572, "browser cannot load it", 14, "#d33")
    line([(500, 515), (500, 610), (723, 610)], "#d33", "arx", dash=True)
    tag(612, 632, "agent cannot read it", 14, "#d33")
    text(500, 672, "EVE received text only.\nIt has no bytes to store.", 14, fill="#444")
    text(825, 672, "The container may stop\nand take the file with it.", 13, fill="#444")
    save("../images/file-path-failure.svg")


def fixed():
    start(1000, 520, "The middleware attaches the file. EVE's interceptor writes it to an /artifacts path that the agent reads "
                     "and the browser loads.")
    rect(20, 20, 960, 480, "#eefaf2", "#3aa76d")
    text(40, 52, "Deployed with the launcher and EVE's interceptor. The same code works.", 18, "bold", "start")
    machine(40, 75, 270, 405, "Machine A", "the user's browser")
    machine(365, 75, 270, 405, "Machine B", "EVE backend")
    machine(690, 75, 270, 405, "Machine C", "MCP server container")
    box(75, 145, 200, 80, "Chat")
    box(400, 145, 200, 80, "ArtifactInterceptor", "bytes to a file")
    filebox(400, 270, 200, "map.png", "/artifacts/{id}/map.png")
    box(400, 385, 200, 64, "Agent")
    box(725, 145, 200, 80, "OutputFilesMiddleware", "path to bytes")
    box(725, 270, 200, 64, "Server", "your code, unchanged")
    filebox(725, 385, 200, "map.png", "/tmp/effis/")
    line([(825, 334), (825, 383)])
    line([(825, 270), (825, 227)])
    text(838, 253, "the path", 13, anchor="start")
    line([(725, 170), (602, 170)])
    tag(663, 160, "the bytes")
    line([(500, 225), (500, 268)])
    text(512, 252, "writes", 13, anchor="start")
    line([(500, 385), (500, 336)], "#c9a400", "arf", dash=True)
    text(512, 365, "reads the path", 13, anchor="start")
    line([(400, 417), (140, 417), (140, 227)])
    tag(270, 408, "answer links the path")
    line([(210, 225), (210, 302), (398, 302)], "#c9a400", "arf", dash=True)
    tag(304, 293, "loads the file")
    save("../images/file-path-middleware.svg")


if __name__ == "__main__":
    failure()
    fixed()
