#!/usr/bin/env python3
"""Self-contained Markdown -> HTML converter for the hlsl_interpreter docs.

No third-party deps (project is stdlib-only). Converts every Sessions/*.md and
Prompts/*.md into a styled, self-contained HTML page under Docs/, and wires the
two prompt logs' steps to their corresponding Session HTML pages.
"""
import os
import re
import html
import glob

ROOT = os.path.dirname(os.path.abspath(__file__))
SESS_DIR = os.path.join(ROOT, 'Sessions')
PROMPT_DIR = os.path.join(ROOT, 'Prompts')
DOCS = os.path.join(ROOT, 'Docs')
OUT_SESS = os.path.join(DOCS, 'Sessions')
OUT_PROMPT = os.path.join(DOCS, 'Prompts')

CSS = """
:root{--bg:#0e1116;--panel:#161b22;--panel2:#1c232d;--line:#2a2f3a;--accent:#5db0ff;
  --green:#46d39a;--amber:#e0a93b;--red:#ff6b6b;--muted:#9aa4b2;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:#d4dae4;font:15px/1.7 -apple-system,Segoe UI,Roboto,Helvetica,Arial,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:0 22px 80px}
.top{position:sticky;top:0;background:rgba(14,17,22,.92);backdrop-filter:blur(6px);
  border-bottom:1px solid var(--line);padding:12px 22px;font-size:14px}
.top a{color:var(--accent);text-decoration:none}.top a:hover{text-decoration:underline}
.top .crumb{color:var(--muted)}
h1{font-size:27px;margin:30px 0 14px;line-height:1.3}
h2{font-size:22px;margin:40px 0 12px;border-left:4px solid var(--accent);padding-left:12px}
h3{font-size:18px;margin:26px 0 9px;color:#cdd6e4}
h4{font-size:15.5px;margin:20px 0 8px;color:#cdd6e4}
h2 a.sess,h1 a.sess{font-size:13px;font-family:var(--mono);font-weight:normal;color:var(--green);
  border:1px solid var(--line);border-radius:16px;padding:2px 10px;margin-left:10px;
  text-decoration:none;white-space:nowrap;vertical-align:middle}
h2 a.sess:hover,h1 a.sess:hover{background:var(--panel);color:#7fe3bb}
p,li{color:#d4dae4}
a{color:var(--accent)}
code{font-family:var(--mono);font-size:13px;background:#11151c;border:1px solid var(--line);
  border-radius:5px;padding:1px 6px;color:#bfe1ff}
pre{background:#0c0f14;border:1px solid var(--line);border-radius:10px;padding:13px 15px;overflow:auto}
pre code{background:none;border:none;padding:0;color:#cfe3f5;font-size:12.5px;line-height:1.55}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{background:var(--panel2);color:#cdd6e4}
tr:nth-child(even) td{background:#13161d}
blockquote{margin:14px 0;padding:8px 16px;border-left:4px solid var(--accent);
  background:#12202e;border-radius:8px;color:#c7d2de}
hr{border:none;border-top:1px solid var(--line);margin:26px 0}
ul,ol{padding-left:26px}
.sesslink{display:inline-block;margin:4px 0;font-family:var(--mono);font-size:13px}
"""

# ---------------------------------------------------------------------------
# Build step-number -> session html filename index.
# Two prefixes exist: hlsl-step{N}- (early/OpenCode era) and
# hlsl-interpreter-step{N}- (later). Keep both; also map exact filenames.
# ---------------------------------------------------------------------------
bare_idx = {}    # N -> stem  (hlsl-step{N}-)
interp_idx = {}  # N -> stem  (hlsl-interpreter-step{N}-)
exact = set()    # set of stems that exist

for p in glob.glob(os.path.join(SESS_DIR, '*.md')):
    stem = os.path.splitext(os.path.basename(p))[0]
    exact.add(stem)
    m = re.match(r'hlsl-interpreter-step(\d+)-', stem)
    if m:
        interp_idx.setdefault(int(m.group(1)), stem)
        continue
    m = re.match(r'hlsl-step(\d+)-', stem)
    if m:
        bare_idx.setdefault(int(m.group(1)), stem)


def session_for_step(n):
    """Pick the session stem for a step number, preferring the bare/early prefix."""
    if n in bare_idx:
        return bare_idx[n]
    if n in interp_idx:
        return interp_idx[n]
    return None


# ---------------------------------------------------------------------------
# Inline markdown -> html.
# ---------------------------------------------------------------------------
SESS_PATH_RE = re.compile(r'Sessions/([A-Za-z0-9._-]+)\.md')


def _sess_href(name, depth_prefix):
    """name = session stem (no extension). depth_prefix='../' for prompt pages."""
    return f'{depth_prefix}Sessions/{name}.html'


def inline(text, sess_prefix):
    """Render inline markdown on one line of text. sess_prefix is the relative
    path prefix to reach Docs/Sessions/ (e.g. '../' from Docs/Prompts/, '' from
    within Docs/Sessions/)."""
    # 1. pull out code spans
    spans = []
    def grab(m):
        spans.append(m.group(1))
        return f'\x00{len(spans)-1}\x00'
    text = re.sub(r'`([^`]+)`', grab, text)

    # 2. escape
    text = html.escape(text, quote=False)

    # 3. markdown links [t](u)
    def link(m):
        t, u = m.group(1), m.group(2)
        # rewrite local .md links to .html
        if u.startswith('Sessions/') and u.endswith('.md'):
            nm = u[len('Sessions/'):-3]
            u = _sess_href(nm, sess_prefix)
        elif u.endswith('.md') and '/' not in u:
            u = u[:-3] + '.html'
        return f'<a href="{html.escape(u,quote=True)}">{t}</a>'
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, text)

    # 4. bold / italic
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<![\w*])\*([^*\n]+)\*(?![\w*])', r'<i>\1</i>', text)

    # 5. autolink bare Sessions/...md mentions (outside code spans)
    def autop(m):
        nm = m.group(1)
        return f'<a href="{_sess_href(nm, sess_prefix)}">Sessions/{nm}.html</a>'
    text = SESS_PATH_RE.sub(autop, text)

    # 6. restore code spans (escaping their content); linkify session paths inside
    def restore(m):
        raw = spans[int(m.group(1))]
        esc = html.escape(raw, quote=False)
        sm = SESS_PATH_RE.fullmatch(raw)
        code = f'<code>{esc}</code>'
        if sm:
            return f'<a href="{_sess_href(sm.group(1), sess_prefix)}">{code}</a>'
        return code
    text = re.sub(r'\x00(\d+)\x00', restore, text)
    return text


# ---------------------------------------------------------------------------
# Block-level markdown -> html.
# header_link(level, raw_heading_text) may return an html anchor string to
# append to the heading (used to wire prompt steps to sessions).
# ---------------------------------------------------------------------------
def convert_body(md, sess_prefix, header_link=None, anchor_steps=False):
    lines = md.replace('\r\n', '\n').split('\n')
    out = []
    i = 0
    n = len(lines)

    def flush_para(buf):
        if buf:
            out.append('<p>' + '<br>'.join(inline(b, sess_prefix) for b in buf) + '</p>')
            buf.clear()

    para = []
    while i < n:
        line = lines[i]

        # fenced code block
        m = re.match(r'^\s*```(.*)$', line)
        if m:
            flush_para(para)
            i += 1
            buf = []
            while i < n and not re.match(r'^\s*```\s*$', lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            code = html.escape('\n'.join(buf), quote=False)
            out.append(f'<pre><code>{code}</code></pre>')
            continue

        # heading
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            flush_para(para)
            lvl = len(m.group(1))
            txt = m.group(2).strip()
            extra = ''
            if header_link:
                extra = header_link(lvl, txt) or ''
            attr = ''
            if anchor_steps and lvl == 1:
                sm = re.match(r'^(\d+)', txt)
                if sm:
                    attr = f' id="step-{sm.group(1)}"'
            out.append(f'<h{lvl}{attr}>{inline(txt, sess_prefix)}{extra}</h{lvl}>')
            i += 1
            continue

        # horizontal rule
        if re.match(r'^\s*([-*_])\1{2,}\s*$', line):
            flush_para(para)
            out.append('<hr>')
            i += 1
            continue

        # table: a header row followed by a |---|---| separator
        if line.lstrip().startswith('|') and i + 1 < n and \
                re.match(r'^\s*\|?[\s:|-]+\|[\s:|-]*$', lines[i + 1]) and '-' in lines[i + 1]:
            flush_para(para)
            def cells(row):
                row = row.strip()
                if row.startswith('|'):
                    row = row[1:]
                if row.endswith('|'):
                    row = row[:-1]
                return [c.strip() for c in row.split('|')]
            header = cells(line)
            i += 2
            rows = []
            while i < n and lines[i].lstrip().startswith('|'):
                rows.append(cells(lines[i]))
                i += 1
            t = ['<table>', '<tr>' + ''.join(f'<th>{inline(c, sess_prefix)}</th>' for c in header) + '</tr>']
            for r in rows:
                t.append('<tr>' + ''.join(f'<td>{inline(c, sess_prefix)}</td>' for c in r) + '</tr>')
            t.append('</table>')
            out.append('\n'.join(t))
            continue

        # blockquote
        if re.match(r'^\s*>', line):
            flush_para(para)
            buf = []
            while i < n and re.match(r'^\s*>', lines[i]):
                buf.append(re.sub(r'^\s*>\s?', '', lines[i]))
                i += 1
            out.append('<blockquote>' +
                       '<br>'.join(inline(b, sess_prefix) for b in buf if b.strip() != '') +
                       '</blockquote>')
            continue

        # lists (ordered / unordered) with simple nesting by indent
        if re.match(r'^\s*([-*+]|\d+\.)\s+', line):
            flush_para(para)
            items = []  # (indent, ordered, text)
            while i < n and re.match(r'^\s*([-*+]|\d+\.)\s+', lines[i]):
                lm = re.match(r'^(\s*)([-*+]|\d+\.)\s+(.*)$', lines[i])
                indent = len(lm.group(1))
                ordered = bool(re.match(r'\d+\.', lm.group(2)))
                text = lm.group(3)
                i += 1
                # gather continuation / nested deeper lines into this item later;
                # keep simple: continuation plain lines (more indented, not a bullet)
                while i < n and lines[i].strip() != '' and \
                        not re.match(r'^\s*([-*+]|\d+\.)\s+', lines[i]) and \
                        lines[i].startswith(' '):
                    text += ' ' + lines[i].strip()
                    i += 1
                items.append((indent, ordered, text))
            out.append(render_list(items, 0, sess_prefix))
            continue

        # blank line -> paragraph break
        if line.strip() == '':
            flush_para(para)
            i += 1
            continue

        # plain paragraph line
        para.append(line)
        i += 1

    flush_para(para)
    return '\n'.join(out)


def render_list(items, start, sess_prefix):
    """Render items (indent, ordered, text) into nested <ul>/<ol> from index start.
    Returns html; consumes a contiguous block at the base indent of items[start]."""
    if start >= len(items):
        return ''
    base = items[start][0]
    ordered = items[start][1]
    tag = 'ol' if ordered else 'ul'
    html_out = [f'<{tag}>']
    i = start
    while i < len(items):
        indent, od, text = items[i]
        if indent < base:
            break
        if indent > base:
            # nested: attach to previous <li>
            sub = render_list(items, i, sess_prefix)
            # find how many consumed
            j = i
            while j < len(items) and items[j][0] > base:
                j += 1
            # append sub-list inside last li
            if html_out and html_out[-1].endswith('</li>'):
                html_out[-1] = html_out[-1][:-5] + sub + '</li>'
            else:
                html_out.append(sub)
            i = j
            continue
        html_out.append(f'<li>{inline(text, sess_prefix)}</li>')
        i += 1
    html_out.append(f'</{tag}>')
    return '\n'.join(html_out)


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style></head>
<body>
<div class="top"><a href="{home}">← 文档首页</a> <span class="crumb">/ {crumb}</span></div>
<div class="wrap">
{body}
</div></body></html>
"""


def title_of(md, fallback):
    for line in md.split('\n'):
        m = re.match(r'^#\s+(.*)$', line)
        if m:
            return re.sub(r'[*`]', '', m.group(1)).strip()
    return fallback


def write_page(out_path, title, body, home, crumb):
    htmlpage = PAGE.format(title=html.escape(title), css=CSS, body=body,
                           home=home, crumb=html.escape(crumb))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(htmlpage)

# ===========================================================================
# Driver: convert all Sessions/ and Prompts/ markdown into Docs/*.html
# ===========================================================================
os.makedirs(OUT_SESS, exist_ok=True)
os.makedirs(OUT_PROMPT, exist_ok=True)

# ---- 1. Convert every Sessions/*.md -> Docs/Sessions/*.html ----------------
sess_files = sorted(glob.glob(os.path.join(SESS_DIR, '*.md')))
for p in sess_files:
    stem = os.path.splitext(os.path.basename(p))[0]
    md = open(p, encoding='utf-8').read()
    body = convert_body(md, sess_prefix='')  # already inside Docs/Sessions/
    title = title_of(md, stem)
    write_page(os.path.join(OUT_SESS, stem + '.html'), title, body,
               home='../index.html', crumb=f'Sessions / {stem}')
print(f'Sessions: {len(sess_files)} html written')

# ---- 2. Convert Prompts/*.md -> Docs/Prompts/*.html with step links --------
# The two prompt logs need different linking strategies (their '# N' headers
# are different things):
#   * ClaudeCode: '# N' is a prompt-session counter (1-47, 89-95, 122, 150-200),
#     NOT a step number. It contains 34 explicit `Sessions/<stem>.md` references
#     naming exactly which session each step produced -> we linkify those inline
#     (handled by convert_body's autolink). No header badge needed.
#   * OpenCode: '# N' is a prompt counter that DRIFTS from the step number
#     (#89 == step81). No inline refs at all. We map each block to its session
#     by matching the prompt TEXT (the session stores the same prompt under
#     '## User'), then add a '-> Session' badge to the '# N' heading.
_CJK = re.compile(r'[一-鿿]')
def _norm(s):
    return re.sub(r'\s+', '', s)

def _session_norm_bodies():
    out = {}
    for p in glob.glob(os.path.join(SESS_DIR, '*.md')):
        stem = os.path.splitext(os.path.basename(p))[0]
        out[stem] = _norm(open(p, encoding='utf-8').read())
    return out

def _session_fingerprints(md):
    """Distinctive Chinese instruction lines from a session's ## User prompt."""
    m = re.search(r'^##\s+User\s*$', md, re.M)
    body = md[m.end():] if m else md
    nm = re.search(r'^##\s+\w', body, re.M)
    if nm:
        body = body[:nm.start()]
    fps = [_norm(l.strip())[:40] for l in body.split('\n')
           if len(l.strip()) >= 12 and _CJK.search(l)]
    return fps[:6]

def build_opencode_block_map(prompt_md):
    """Return {block_number: session_stem} by matching prompt text both ways."""
    # parse '# N' blocks
    raw = {}
    cur, buf = None, []
    for line in prompt_md.split('\n'):
        m = re.match(r'^# (\d+)( |$)', line)
        if m:
            if cur is not None:
                raw[cur] = '\n'.join(buf)
            cur, buf = int(m.group(1)), []
        else:
            buf.append(line)
    if cur is not None:
        raw[cur] = '\n'.join(buf)
    bnorm = {n: _norm(t) for n, t in raw.items()}

    sess_md = {os.path.splitext(os.path.basename(p))[0]: open(p, encoding='utf-8').read()
               for p in glob.glob(os.path.join(SESS_DIR, '*.md'))}
    snorm = {s: _norm(md) for s, md in sess_md.items()}

    def step_key(s):
        mm = re.search(r'step(\d+)', s)
        return int(mm.group(1)) if mm else 99999

    mp, used = {}, set()
    # pass 1 (forward): session fingerprint found in a block
    for stem in sorted(sess_md, key=step_key):
        for fp in _session_fingerprints(sess_md[stem]):
            hit = next((n for n, t in bnorm.items() if n not in used and fp in t), None)
            if hit is not None:
                mp[hit] = stem
                used.add(hit)
                break
    # pass 2 (reverse): unmatched non-empty block's Chinese line found in a session
    mapped_sess = set(mp.values())
    for n in sorted(bnorm):
        if n in mp or len(bnorm[n]) < 20:
            continue
        for bl in [_norm(l.strip())[:40] for l in raw[n].split('\n')
                   if len(l.strip()) >= 16 and _CJK.search(l)]:
            hit = next((s for s in snorm if s not in mapped_sess and bl in snorm[s]), None)
            if hit is not None:
                mp[n] = hit
                mapped_sess.add(hit)
                break
    return mp

_OPENCODE_MAP = {}

def claude_header_link(level, txt):
    return ''  # ClaudeCode links via inline `Sessions/...md` refs

def opencode_header_link(level, txt):
    if level != 1:
        return ''
    m = re.match(r'^(\d+)', txt.strip())
    if not m:
        return ''
    stem = _OPENCODE_MAP.get(int(m.group(1)))
    if stem:
        return f' <a class="sess" href="../Sessions/{stem}.html">→ Session</a>'
    return ''

prompt_specs = [
    ('hlsl-interpreter-prompt-ClaudeCode.md', claude_header_link),
    ('hlsl-interpreter-prompt-OpenCode.md', opencode_header_link),
]
for fname, hl in prompt_specs:
    p = os.path.join(PROMPT_DIR, fname)
    if not os.path.exists(p):
        continue
    stem = os.path.splitext(fname)[0]
    md = open(p, encoding='utf-8').read()
    if 'OpenCode' in fname:
        _OPENCODE_MAP = build_opencode_block_map(md)
        globals()['_OPENCODE_MAP'] = _OPENCODE_MAP
        print(f'  OpenCode step→session matches: {len(_OPENCODE_MAP)}')
    body = convert_body(md, sess_prefix='../', header_link=hl, anchor_steps=True)
    title = title_of(md, stem)
    write_page(os.path.join(OUT_PROMPT, stem + '.html'), title, body,
               home='../index.html', crumb=f'Prompts / {stem}')
    print(f'Prompt: {stem}.html written')

# ---- 3. Index page ----------------------------------------------------------
def step_num(stem):
    m = re.search(r'step(\d+)', stem)
    return int(m.group(1)) if m else 99999

sess_links = []
for p in sorted(sess_files, key=lambda x: step_num(os.path.basename(x))):
    stem = os.path.splitext(os.path.basename(p))[0]
    md = open(p, encoding='utf-8').read()
    t = title_of(md, stem)
    sess_links.append(f'<li><a href="Sessions/{stem}.html">{html.escape(t)}</a> '
                      f'<span style="color:#6b7480;font-size:12px">{stem}</span></li>')

idx_body = f"""<h1>HLSL Interpreter 开发文档</h1>
<h2>手册</h2>
<ul>
<li><a href="AI-Development-Handbook.html">AI 辅助开发手册</a></li>
</ul>
<h2>提示词历史 (Prompts)</h2>
<ul>
<li><a href="Prompts/hlsl-interpreter-prompt-ClaudeCode.html">Claude Code Session 提示词历史</a>（每步经 <code>Sessions/...md</code> 引用链接到对应 Session）</li>
<li><a href="Prompts/hlsl-interpreter-prompt-OpenCode.html">OpenCode Session 提示词历史</a>（每步标题带 → Session 链接）</li>
</ul>
<h2>开发步骤日志 (Sessions, {len(sess_links)})</h2>
<ol style="line-height:1.9">
{os.linesep.join(sess_links)}
</ol>
"""
write_page(os.path.join(DOCS, 'index.html'), 'HLSL Interpreter 开发文档',
           idx_body, home='index.html', crumb='首页')
print('index.html written')
