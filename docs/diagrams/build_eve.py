"""Build the handbook diagrams in the EVE style.

Flow diagrams are laid out by hand with the diagram-generator skill's eve_svg builder.
Sequence diagrams stay Mermaid (see the .mmd files here) and get the EVE theme injected.

    MMDC=/path/to/mmdc python docs/diagrams/build_eve.py            # all
    python docs/diagrams/build_eve.py --only transports react-loop  # some
    python docs/diagrams/build_eve.py --no-mermaid                  # skip sequence diagrams

Needs: the skill at ~/.cursor/skills/diagram-generator, rsvg-convert only for --png previews.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(os.environ.get("DIAGRAM_SKILL", Path.home() / ".cursor/skills/diagram-generator")) / "scripts"
sys.path.insert(0, str(SKILL))
from eve_svg import Canvas  # noqa: E402
import diagram_style  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "images"
STYLE = "eve"


def new(**kw) -> Canvas:
    return Canvas(style=STYLE, **kw)


# ------------------------------------------------------------------ flows

def registry_architecture() -> Canvas:
    c = new()
    agent_y, agent_h = 213, 80
    agent_cy = agent_y + agent_h / 2
    user = c.person(20, agent_cy - 26, "EVE user")
    host = c.cluster(130, 30, 360, 440, "MCP host: EVE", header=True)
    agent = c.node(150, agent_y, "ReAct agent\n(LLM + loop)", w=130, h=agent_h)
    # Client, server, and upstream centres share one y per row, so those arrows stay horizontal.
    row_cy = [115, agent_cy, 391]
    clients = [c.node(330, cy - 35, f"MCP client\n{n}", w=130, h=70) for cy, n in zip(row_cy, ["geocode", "effis", "serpapi"])]
    reg = c.cluster(640, 30, 300, 440, "Registry servers", header=True)
    # First line is the server name (the folder under servers/). Lines under it are tools.
    servers = [c.node(660, cy - 45, t, w=260, h=90) for cy, t in zip(row_cy, [
        "Server: geocode\ngeocode_place\nreverse_geocode_place",
        "Server: effis\nget_effis_burnt_areas\ncompute_metrics",
        "Server: serpapi\nsearch_google"])]
    up = c.cluster(1080, 30, 220, 440, "Upstream services", header=True)
    dbs = [c.db(1156, cy - 29, t, w=48, h=58) for cy, t in zip(row_cy, [
        "OpenStreetMap\nNominatim", "EFFIS WFS\nCopernicus Data Space", "SerpAPI\nGoogle Search"])]
    c.arrow(user, "right", agent, "left")
    c.arrow(agent, "right", clients[1], "left")
    # Top and bottom clients sit outside the agent box, so the bend is one elbow, not a slant.
    for cl, dy in ((clients[0], -28), (clients[2], 28)):
        c.arrow(agent, "right", cl, "left", da=dy)
    for cl, s in zip(clients, servers):
        c.arrow(cl, "right", s, "left", label="Streamable HTTP")
    for s, d in zip(servers, dbs):
        c.arrow(s, "right", d, "left")
    return c


def participants() -> Canvas:
    c = new()
    c.cluster(30, 20, 800, 140, "MCP Host (AI application)", header=True)
    cl = [c.node(60 + k * 190, 80, f"MCP Client {k + 1}", w=160, h=64) for k in range(4)]
    texts = ["MCP Server A - Local\n(e.g. Filesystem)", "MCP Server B - Local\n(e.g. Database)", "MCP Server C - Remote\n(e.g. Sentry)"]
    sa = c.node(60, 330, texts[0], w=160, h=76, role="slate")
    sb = c.node(250, 330, texts[1], w=160, h=76, role="slate")
    sc = c.node(535, 330, texts[2], w=200, h=76, role="slate")
    lab = "Dedicated\nconnection"
    c.arrow(cl[0], "bottom", sa, "top", label=lab)
    c.arrow(cl[1], "bottom", sb, "top", label=lab)
    c.arrow(cl[2], "bottom", sc, "top", label=lab, db=-40, mid=290)
    c.arrow(cl[3], "bottom", sc, "top", label=lab, db=40, mid=290)
    return c


def registry_lifecycle() -> Canvas:
    c = new()
    w, h = 190, 84
    # One teal for every step. Dark only for the entry and the exit.
    xs = [40, 310, 580, 850]
    y1, y_rev, y2 = 70, 248, 400
    dev = c.node(xs[0], y1, "servers/your-server/", w=w, h=h, role="terminal")
    pr = c.node(xs[1], y1, "Pull request\nagainst main", w=w, h=h)
    chk = c.node(xs[2], y1, "PR checks", w=w, h=h)
    # Scope gate before code review. Same column, so the pass drops straight down.
    dom = c.node(xs[3], y1, "Domain review\nEO, related science", w=w, h=h)
    rev = c.node(xs[3], y_rev, "Code review", w=w, h=h)
    # Merge and the running server are one step.
    live = c.node(xs[3], y2, "Merge to main\nServer runs", w=w, h=h)
    stage = c.node(xs[2], y2, "Staging test", w=w, h=h)
    # Final gate. Same column as the pull request, so a denial goes straight back up.
    lab = c.node(xs[1], y2, "ESA \u03a6-lab\nFinal approval", w=w, h=h)
    public = c.node(xs[0], y2, "Public in EVE", w=w, h=h, role="terminal")
    c.note_under(dev, "server.py, requirements.txt,\nCITATION.cff")
    c.note_under(chk, "pr-checks.yml")
    c.note_under(live, "deployed and running")
    c.note_under(public, "tools/list shows it")
    pw, ph = 34, 40
    # People on the top edge. Code review's icon sits on the right so the pass arrow can land in the centre.
    for step in (dom, stage, lab):
        c.person(step.cx - pw / 2, step.y - ph + 14, w=pw, h=ph)
    c.person(rev.x + rev.w - pw, rev.y - ph + 10, w=pw, h=ph)
    c.arrow(dev, "right", pr, "left")
    c.arrow(pr, "right", chk, "left")
    c.arrow(chk, "right", dom, "left", label="pass")
    c.arrow(chk, "top", dev, "top", kind="error", via=[(chk.cx, 28), (dev.cx, 28)], label="fail")
    c.arrow(dom, "bottom", rev, "top", label="pass")
    # Out of scope returns to the pull request. Another topic is not revised, it is refused.
    scope_y = 228
    c.arrow(
        dom, "bottom", pr, "bottom", kind="error", label="out of scope",
        da=-75, db=65,
        via=[(dom.cx - 75, scope_y), (pr.cx + 65, scope_y)],
    )
    # A code rejection is an error back to the same pull request.
    c.arrow(
        rev, "left", pr, "bottom", kind="error", label="reject",
        via=[(pr.cx, rev.cy)],
    )
    c.arrow(rev, "bottom", live, "top", label="pass")
    c.arrow(live, "left", stage, "right")
    c.arrow(stage, "left", lab, "right")
    c.arrow(lab, "left", public, "right", label="approve")
    # A denial sends the whole contribution back to the pull request. The label sits on the lower half, clear of the other returns.
    deny_x = lab.cx - 90
    c.arrow(
        lab, "top", pr, "bottom", kind="error", label="deny",
        da=-90, db=-90,
        via=[(deny_x, 250)],
    )
    return c


def server_folder() -> Canvas:
    """One server folder. Each file is a single line."""
    c = new()
    rows = [
        ("server.py", "FastMCP on port 8000"),
        ("requirements.txt", "mcp[cli]==1.27.0"),
        ("CITATION.cff", "title and authors"),
        (".env.template", "KEY=${KEY}"),
        ("test.py", "call the tools"),
        ("README.md", "citation, after merge"),
        ("Dockerfile", "replaces the shared one"),
    ]
    row_h, gap = 40, 8
    top = 68
    height = top + len(rows) * (row_h + gap) + 4
    c.cluster(16, 16, 520, height, "servers/your-server/", header=True)
    for i, (name, line) in enumerate(rows):
        y = 16 + top + i * (row_h + gap)
        c.node(36, y, name, w=200, h=row_h, role="code")
        c.note(252, y + 3, line, w=260)
    return c


def credentials() -> Canvas:
    """How a server gets each user's own credentials (section 3.6)."""
    c = new()
    w, h = 200, 78
    c.cluster(16, 16, 900, 250, "1. When the server is deployed", header=True)
    c.cluster(16, 300, 900, 210, "2. When a user adds credentials", header=True)
    c.cluster(16, 544, 900, 330, "3. When a tool is called", header=True)

    dep = c.node(40, 78, "Server is deployed", w=w, h=h, role="terminal")
    fnd = c.node(310, 78, "EVE finds\n.env.template", w=w, h=h, role="code")
    mark = c.node(580, 78, "Server is marked as\nneeding credentials", w=w, h=h)
    c.note_under(fnd, "CDSE_CLIENT_ID=${CDSE_CLIENT_ID}", gap=12)
    c.arrow(dep, "right", fnd, "left")
    c.arrow(fnd, "right", mark, "left")

    # The user is the person icon. The boxes are the actions, not another portrait.
    usr = c.person(150, 392, "User")
    add = c.node(280, 378, "Enters\nCDSE_CLIENT_ID", w=w, h=h)
    sto = c.node(560, 378, "EVE stores the value", w=w, h=h, role="slate")
    c.arrow(usr, "right", add, "left")
    c.arrow(add, "right", sto, "left")

    call = c.node(40, 620, "Tool is called", w=w, h=h, role="terminal")
    chk = c.node(300, 620, "Credentials set?", w=w, h=h, role="decision")
    ok = c.node(580, 620, "Tool runs with\nthe stored value", w=w, h=h)
    ask = c.node(300, 790, "EVE asks for\nCDSE_CLIENT_ID", w=w, h=h, role="highlight")
    save = c.node(580, 790, "Value is stored,\nthe call is retried", w=w, h=h, role="slate")
    pw, ph = 34, 40
    c.person(ask.x - 48, ask.cy - ph / 2, w=pw, h=ph)
    c.arrow(call, "right", chk, "left")
    c.arrow(chk, "right", ok, "left", label="yes")
    c.arrow(chk, "bottom", ask, "top", label="no")
    c.arrow(ask, "right", save, "left")
    c.arrow(save, "top", ok, "bottom")
    return c

def tool_anatomy() -> Canvas:
    c = new()
    c.cluster(20, 20, 380, 540, "Python, under @mcp.tool()", header=True)
    c.cluster(600, 20, 380, 540, "One entry in tools/list", header=True)
    left = ["Function name\nlookup_aoi", "Docstring\nsummary, Args, Returns",
            "Parameters\nAnnotated[str, Field(description=...)]", "Return annotation\n-> AOI (TypedDict)",
            "annotations=\nToolAnnotations(...)"]
    right = ["name", "description", "inputSchema\nproperties, required, ranges", "outputSchema",
             "annotations\nreadOnlyHint, openWorldHint"]
    for k, (l, r) in enumerate(zip(left, right)):
        y = 75 + k * 95
        a = c.node(40, y, l, w=340, h=70)
        b = c.node(620, y, r, w=340, h=70, role="slate")
        c.arrow(a, "right", b, "left")
    return c


def tool_errors() -> Canvas:
    c = new()
    req = c.node(300, 20, "tools/call lookup_aoi\nplace = 'Atlantis'", w=240, h=64, role="terminal")
    d1 = c.node(280, 140, "Known tool and\nwell-formed request?", w=280, h=84, role="decision")
    proto = c.node(10, 147, "Protocol error\nJSON-RPC error -32602", w=220, h=70, role="danger")
    fix = c.node(10, 290, "The host handles it.\nThe model can rarely fix it.", w=220, h=70, role="note")
    run = c.node(320, 300, "Run the tool", w=200, h=56)
    d2 = c.node(280, 410, "Valid arguments and\na usable answer?", w=280, h=84, role="decision")
    res = c.node(300, 590, "Result\ncontent + structuredContent", w=240, h=70, role="terminal")
    exe = c.node(660, 417, "Tool execution error\nisError: true", w=280, h=70, role="danger")
    exn = c.note_under(exe, "Unknown area Atlantis.\nKnown areas: attica, evia.", gap=10)
    retry = c.node(660, 590, "The model reads the text\nand retries with new arguments", w=280, h=70)
    c.arrow(req, "bottom", d1, "top")
    c.arrow(d1, "left", proto, "right", label="no")
    c.arrow(proto, "bottom", fix, "top")
    c.arrow(d1, "bottom", run, "top", label="yes")
    c.arrow(run, "bottom", d2, "top")
    c.arrow(d2, "right", exe, "left", label="no")
    c.arrow(d2, "bottom", res, "top", label="yes")
    c.arrow(exn, "bottom", retry, "top")
    c.arrow(retry, "right", req, "right", mid=1010)
    return c


def transports() -> Canvas:
    c = new()
    c.cluster(20, 20, 960, 360, "Remote: Streamable HTTP", header=True)
    eve = c.node(50, 170, "EVE\n(host)", w=130, h=76)
    rt = c.node(350, 170, "AgentCore\nruntime", w=170, h=76)
    r1 = c.node(600, 90, "Container replica 1\nstateless_http=True", w=220, h=70)
    r2 = c.node(600, 256, "Container replica 2\nstateless_http=True", w=220, h=70)
    logs = c.note(870, 183, "process\nlogs")
    c.arrow(eve, "right", rt, "left", label="POST /mcp\nplus auth headers")
    c.arrow(rt, "right", r1, "left")
    c.arrow(rt, "right", r2, "left")
    c.arrow(r1, "right", logs, "top", kind="async")
    c.arrow(r2, "right", logs, "bottom", kind="async")
    c.cluster(20, 420, 960, 250, "Local: stdio", header=True)
    ide = c.node(50, 520, "Cursor or Claude Desktop\n(host)", w=240, h=110)
    proc = c.node(560, 520, "python server.py\n--transport stdio", w=200, h=110)
    err = c.note(850, 556, "stderr\nlogs")
    c.arrow(ide, "right", proc, "left", da=-34, db=-34, label="starts the process")
    c.arrow(ide, "right", proc, "left", da=0, db=0, label="JSON-RPC on stdin")
    c.arrow(proc, "left", ide, "right", da=34, db=34, label="JSON-RPC on stdout")
    c.arrow(proc, "right", err, "left", kind="async")
    return c


def react_loop() -> Canvas:
    c = new()
    user = c.node(190, 20, "User query", w=220, h=56, role="terminal")
    llm1 = c.node(160, 130, "LLM reasons\ndecides a tool is needed,\nemits a structured tool call", w=280, h=92)
    host = c.node(160, 290, "Host application\nexecutes the tool", w=280, h=70, role="slate")
    llm2 = c.node(160, 430, "LLM receives the result\nreasons again, may call\nanother tool", w=280, h=92)
    fin = c.node(160, 640, "LLM emits final response\nnatural language answer\ngrounded in real data", w=280, h=92, role="terminal")
    c.arrow(user, "bottom", llm1, "top")
    c.arrow(llm1, "bottom", host, "top", label="tool call")
    c.arrow(host, "bottom", llm2, "top", label="returns the result")
    c.arrow(llm2, "right", llm1, "right", mid=560, label="repeats\nuntil done")
    c.arrow(llm2, "bottom", fin, "top", label="No more tools needed")
    return c


def progressive_discovery() -> Canvas:
    c = new()
    c.cluster(20, 20, 400, 640, "All tools loaded upfront", header=True)
    tools = ["gdrive.getDocument: schema, params...", "gdrive.listFiles: schema, params...",
             "gdrive.shareFile: schema, params...", "slack.postMessage: schema, params...",
             "slack.getChannels: schema, params...", "salesforce.query: schema, params...",
             "salesforce.updateRecord: schema...", "... 150+ more tool definitions ..."]
    for k, t in enumerate(tools):
        c.note(50, 75 + k * 44, t, w=340, size=12)
    q = c.node(50, 450, 'User: "Get my doc and update the lead"', w=340, h=64, role="slate")
    stat = c.node(50, 560, "~150,000 tokens\nbefore the model reads the request", w=340, h=70, role="danger")
    c.arrow(q, "bottom", stat, "top")
    c.cluster(520, 20, 440, 640, "Tools discovered on demand", header=True)
    s1 = c.node(560, 85, '1. Catalog\nsearch_tools("salesforce update")', w=360, h=64)
    n1 = c.note_under(s1, "salesforce.updateRecord, salesforce.query", gap=10, size=12)
    s2 = c.node(560, 255, '2. Inspect\nget_tool_details("salesforce.updateRecord")', w=360, h=64)
    n2 = c.note_under(s2, "full schema, params, return type", gap=10, size=12)
    s3 = c.node(560, 425, "3. Call\nsalesforce.updateRecord({...})", w=360, h=64)
    n3 = c.note_under(s3, "executes with full context", gap=10, size=12)
    stat2 = c.node(600, 560, "~2,000 tokens\nonly the tools needed for this task", w=280, h=70, role="terminal")
    c.arrow(n1, "bottom", s2, "top")
    c.arrow(n2, "bottom", s3, "top")
    c.arrow(n3, "bottom", stat2, "top")
    return c


def file_model_view() -> Canvas:
    """What each hop sends for a returned file (section 3.5).

    Source: shared/eve_mcp_runtime/files.py (_strip, _file_block) and backend
    artifact_ingestion.py (_fold_output_paths, _stub_text).
    """
    c = new()
    c.cluster(20, 20, 1180, 250, "The result has an object with path: EFFIS", header=True)
    tool = c.code(40, 90, '{"files": [{\n  "name": "severity_map.png",\n  "path": "/tmp/effis/severity_map.png",\n'
                          '  "bytes": 48213}]}', w=330)
    launch = c.code(430, 90, '{"files": [{\n  "name": "severity_map.png",\n  "path": "severity_map.png",\n'
                             '  "bytes": 48213}]}', w=290)
    model = c.code(820, 90, '{"files": [{\n  "name": "severity_map.png",\n  "path": "/artifacts/{id}/severity_map.png",\n'
                            '  "bytes": 48213}]}', w=360)
    c.note_under(tool, "Tool returns", gap=12)
    c.note_under(launch, "Launcher sends, plus the PNG bytes", gap=12)
    c.note_under(model, "Agent sees", gap=12)
    c.arrow(tool, "right", launch, "left")
    c.arrow(launch, "right", model, "left")

    c.cluster(20, 310, 1180, 230, "The result has no object with path: dummy_image", header=True)
    img = c.code(40, 380, "ImageContent\n  mimeType: image/png\n  data: <base64>", w=330)
    stub = c.code(450, 380, '![get_sample_image-1.png](/artifacts/{id} "MCP: dummy_image/get_sample_image")\n'
                            '{"artifact_id": "{id}", "content_type": "image/png",\n'
                            ' "filename": "get_sample_image-1.png", "size_bytes": <size>}', w=730)
    c.note_under(img, "Tool returns", gap=12)
    c.note_under(stub, "Agent sees a stub", gap=12)
    c.arrow(img, "right", stub, "left")
    return c


def programmatic_tool_calling() -> Canvas:
    c = new()
    c.cluster(20, 20, 1000, 370, "Direct tool calling: every result flows back through the model", header=True)
    lg = c.node(40, 130, "Logging\nServer", w=160, h=110)
    llm = c.node(430, 130, "LLM", w=140, h=110)
    tk = c.node(810, 130, "Ticketing\nServer", w=160, h=110)
    c.arrow(llm, "left", lg, "right", da=-30, db=-30, label='getLogs(\n{ level: "error" })')
    c.arrow(lg, "right", llm, "left", da=30, db=30, label="2,000 log entries\n(~50K tokens)")
    c.arrow(llm, "right", tk, "left", da=-30, db=-30, label='createIssue(\n{ title: "Error: timeout" })')
    c.arrow(tk, "left", llm, "right", da=30, db=30, label='{ issueId: "T-101" }')
    c.note_under(llm, "... repeated for each unique error ...", gap=22, size=12)
    c.node(430, 322, "~100K+ tokens", w=140, h=44, role="danger")

    c.cluster(20, 430, 1000, 500, "Programmatic tool calling: the model writes code, a sandbox runs it", header=True)
    llm2 = c.node(350, 485, "LLM", w=300, h=60)
    sb = c.node(350, 700, "Sandbox", w=300, h=110)
    lg2 = c.node(30, 700, "Logging\nServer", w=150, h=110)
    tk2 = c.node(820, 700, "Ticketing\nServer", w=150, h=110)
    c.arrow(llm2, "bottom", sb, "top", da=-100, db=-100, label="execute(code)\n~200 tokens")
    c.arrow(sb, "top", llm2, "bottom", da=100, db=100, label='"Filed 12 tickets\nfrom 2,000 logs"\n~15 tokens returned')
    c.arrow(sb, "left", lg2, "right", da=-30, db=-30, label='getLogs(\n{ level: "error" })')
    c.arrow(lg2, "right", sb, "left", da=30, db=30, label="2,000 entries\nstay in sandbox")
    c.arrow(sb, "right", tk2, "left", da=-30, db=-30, label="createIssue(...)")
    c.arrow(tk2, "left", sb, "right", da=30, db=30, label='{ issueId: "T-101" }')
    c.note_under(sb, "all iterations happen\ninside the sandbox", gap=16, size=12)
    return c

FLOWS = {
    "registry-architecture": registry_architecture,
    "participants": participants,
    "registry-lifecycle": registry_lifecycle,
    "server-folder": server_folder,
    "credentials": credentials,
    "tool-anatomy": tool_anatomy,
    "tool-errors": tool_errors,
    "transports": transports,
    "react-loop": react_loop,
    "progressive-discovery": progressive_discovery,
    "programmatic-tool-calling": programmatic_tool_calling,
    "file-model-view": file_model_view,
}
SEQUENCES = ["elicitation", "primitives-together", "react-evia-trace", "resource-link", "state-handles"]


def build_sequence(name: str) -> None:
    mmdc = os.environ.get("MMDC") or shutil.which("mmdc")
    if not mmdc:
        sys.exit("Mermaid sequence diagrams need mmdc: npm i -g @mermaid-js/mermaid-cli, or set MMDC")
    src = (HERE / f"{name}.mmd").read_text(encoding="utf-8")
    styled = diagram_style.apply_style(src, "mermaid", diagram_style.load_profile(STYLE))
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / f"{name}.mmd"
        f.write_text(styled, encoding="utf-8")
        subprocess.run([mmdc, "-i", str(f), "-o", str(OUT / f"{name}.svg"), "-b", "white"], check=True,
                       capture_output=True)


EVE_ONLY = [
    "registry-architecture",
    "registry-lifecycle",
    "server-folder",
    "credentials",
    "react-evia-trace",
    "file-model-view",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--no-mermaid", action="store_true")
    ap.add_argument("--png", type=Path, help="also write PNG previews into this folder")
    args = ap.parse_args()
    # Generic MCP diagrams keep the original Mermaid look. Only EVE-specific ones are styled by default.
    want = set(args.only or EVE_ONLY)
    for name, fn in FLOWS.items():
        if name in want:
            cv = fn()
            cv.save(OUT / f"{name}.svg")
            if args.png:
                args.png.mkdir(parents=True, exist_ok=True)
                subprocess.run(["rsvg-convert", "-z", "1.5", "-o", str(args.png / f"{name}.png"),
                                str(OUT / f"{name}.svg")], check=True)
            print("flow", name)
    if not args.no_mermaid:
        for name in SEQUENCES:
            if name in want:
                build_sequence(name)
                print("sequence", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
