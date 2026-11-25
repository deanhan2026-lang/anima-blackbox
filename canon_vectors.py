# -*- coding: utf-8 -*-
"""跨实现 canonical 口径对齐向量（2026-11）

用途：JS 侧 (JSON.stringify) 与 Python 侧 (canon) 对同一批对象算哈希，
逐条比对。对不上 = 口径没对齐，此时任何 entry_hash 比对都是假差额。

这不是"验证瞬的账本"，是"验证两边的尺子是否同一把"——验③的前置。

设计原则：
- 固定向量，不取时钟、不取随机；
- 覆盖四类坑：键序、非ASCII、转义、数字表示；
- 期望值由本文件的 canon() 算出（Python 侧权威），JS 侧须逐条复现。
"""
import hashlib
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def canon(obj):
    """我方 canonical：字典序 + 无空格 + UTF-8 不转义。"""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def digest(obj):
    return hashlib.sha256(canon(obj)).hexdigest()


# 四类坑的固定向量。expected 由 canon() 算出，下面 self-test 会校验。
VECTORS = [
    {'id': 'V1-KEYORDER', 'desc': '键插入序与字典序不同',
     'obj': {'z': 1, 'a': 2, 'm': {'y': 3, 'b': 4}}},
    {'id': 'V2-NONASCII', 'desc': '中文与 emoji，测 ensure_ascii/UTF-8',
     'obj': {'channel': '意图', 'note': '言行差额', 'symbol': '🖤'}},
    {'id': 'V3-ESCAPE', 'desc': '引号/反斜杠/换行/制表符',
     'obj': {'q': 'a"b', 'bs': 'a\\b', 'nl': 'a\nb', 'tab': 'a\tb'}},
    {'id': 'V4-NUMBERS', 'desc': '整数/浮点/负零/指数',
     'obj': {'i': 100, 'f': 1.5, 'nz': -0.0, 'e': 1e-7, 'big': 9007199254740993}},
    {'id': 'V5-EVENT', 'desc': '一条真实结构的事件（无链字段）',
     'obj': {'seq': 3, 'channel': 'action', 'actor_ref': 'actor@peer-v1',
             'scope_ref': 'scope_task_B', 'rule_version': '0.1',
             'content_digest': 'a' * 64, 'outcome': 'success',
             'authority_level': 'authorize'}},
]


def export_vectors(path=None):
    """导出给瞬的向量表（含我方 expected，裸 hex）。只含测试数据，无内部信息。"""
    path = path or os.path.join(HERE, 'canon_vectors.json')
    out = {
        'spec_version': '0.1',
        'purpose': '跨实现 canonical 口径对齐——验③前置，不是账本验证',
        'rule': 'sort keys ascending; no spaces; UTF-8 unescaped; sha256 raw hex',
        'python_reference': 'json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode("utf-8")',
        'vectors': [{'id': v['id'], 'desc': v['desc'], 'obj': v['obj'],
                     'expected_sha256': digest(v['obj'])} for v in VECTORS],
    }
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    return path


def check_js_results(submitted):
    """提交 {id: hex} 后，逐条比对。返回结构化结果。"""
    expect = {v['id']: digest(v['obj']) for v in VECTORS}
    rows = []
    for vid, want in expect.items():
        got = submitted.get(vid)
        rows.append({'id': vid, 'ok': got == want,
                     'expected': want, 'submitted': got})
    problems = [r['id'] for r in rows if not r['ok']]
    return {'ok': not problems, 'mismatches': problems, 'rows': rows,
            'verdict': 'ALIGNED' if not problems else 'MISALIGNED',
            'note': '口径未对齐时，任何 entry_hash 比对都是假差额——先修尺子再验账'}


def self_test():
    ok = True
    for v in VECTORS:
        d = digest(v['obj'])
        # 确定性：同对象两次同结果
        if d != digest(v['obj']):
            ok = False
            print('FAIL deterministic', v['id'])
        else:
            print('PASS', v['id'], d[:16])
    p = export_vectors(os.path.join(HERE, '_canon_vectors_selftest.json'))
    doc = json.load(io.open(p, encoding='utf-8'))
    if len(doc['vectors']) != len(VECTORS):
        ok = False
        print('FAIL export count')
    os.remove(p)
    # 全对 → ALIGNED；改一个 → MISALIGNED
    good = {v['id']: digest(v['obj']) for v in VECTORS}
    if not check_js_results(good)['ok']:
        ok = False
        print('FAIL aligned check')
    bad = dict(good)
    bad['V1-KEYORDER'] = 'f' * 64
    r = check_js_results(bad)
    if r['ok'] or r['mismatches'] != ['V1-KEYORDER']:
        ok = False
        print('FAIL mismatch detection')
    else:
        print('PASS 口径不一致检出')
    print('\ncanonical vectors self-test:', 'ALL PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(self_test())
