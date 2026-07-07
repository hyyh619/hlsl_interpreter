#!/usr/bin/env python3
"""Regenerate Docs/AI-Development-Handbook.html from the .md using build_docs."""
import os, sys
ROOT = '/Users/yinghuang/development/hy_code/hlsl_interpreter'
sys.path.insert(0, ROOT)
import build_docs as b

md_path = os.path.join(ROOT, 'Docs', 'AI-Development-Handbook.md')
out_path = os.path.join(ROOT, 'Docs', 'AI-Development-Handbook.html')
md = open(md_path, encoding='utf-8').read()
body = b.convert_body(md, sess_prefix='')
title = 'AI 辅助开发手册 — 以 HLSL 解释器项目为例'
b.write_page(out_path, title, body, home='index.html', crumb='AI 辅助开发手册')
print('handbook html written', out_path)
