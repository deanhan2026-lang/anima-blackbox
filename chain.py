# -*- coding: utf-8 -*-
"""链基元 · append-only 哈希链（2026-11）

独立实现的 append-only 哈希链基元（seq/prev_hash/entry_hash），不依赖任何外部模块。

纯标准库、纯新增。不取系统时钟——时间由调用方显式给（可复算纪律）。
"""
import hashlib
import io
import json
import os


def canon(obj):
    """字典序口径（v2.2 起的 sorted 口径）。"""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def canon_insertion(obj):
    """插入序口径（旧账 seq≤N 的 insertion 口径）。

    JSONL 文件里键序即当时写入序，json.loads 保留该顺序，故可真实重算。
    JS 侧对应 JSON.stringify（对象插入序、数组保序）。
    """
    return json.dumps(obj, sort_keys=False, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def digest(obj, mode='sorted'):
    """对对象做 canonical JSON 的 SHA-256，返回裸 hex。

    mode='sorted'    → 字典序（当前口径）
    mode='insertion' → 文件插入序（旧账口径，JS JSON.stringify 等价）
    混合口径账本按 seq 分段选用（见 verify_chain_segmented）。
    """
    c = canon_insertion(obj) if mode == 'insertion' else canon(obj)
    return hashlib.sha256(c).hexdigest()


def read_entries(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in io.open(path, encoding='utf-8').read().splitlines() if l.strip()]


def append_entry(path, payload):
    """把 payload 作为一条链记录追加。payload 不得自带 seq/prev_hash/entry_hash。"""
    for k in ('seq', 'prev_hash', 'entry_hash'):
        if k in payload:
            raise ValueError('E-CHAIN-RESERVED: payload 不得自带 %s' % k)
    entries = read_entries(path)
    prev = entries[-1]['entry_hash'] if entries else None
    entry = dict(payload)
    entry['seq'] = len(entries) + 1
    entry['prev_hash'] = prev
    entry['entry_hash'] = digest({k: v for k, v in entry.items() if k != 'entry_hash'})
    with io.open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n')
    return entry


def verify_chain(path):
    """校验链完整性：seq 连续、prev 相接、entry_hash 可重算。返回结构化结果。"""
    if not os.path.exists(path):
        return {'ok': False, 'entries': 0, 'problems': ['文件不存在']}
    problems = []
    prev = None
    entries = read_entries(path)
    for i, e in enumerate(entries, 1):
        if e.get('seq') != i:
            problems.append('第%d行 seq=%r 不连续' % (i, e.get('seq')))
        if e.get('prev_hash') != prev:
            problems.append('第%d行 prev_hash 不接上一条' % i)
        want = digest({k: v for k, v in e.items() if k != 'entry_hash'})
        if e.get('entry_hash') != want:
            problems.append('第%d行 entry_hash 重算不符（被改）' % i)
        prev = e.get('entry_hash')
    return {'ok': not problems, 'entries': len(entries), 'problems': problems,
            'head': prev}


def verify_chain_segmented(path, legacy_till=0):
    """混合口径整链校验。

    seq <= legacy_till → insertion 口径（旧账，JS JSON.stringify 插入序）
    seq >  legacy_till → sorted 口径（字典序，v2.2 起）

    legacy_till=0 等价于全 sorted（即 verify_chain 的默认行为）。
    返回结构含 segments 明细，供验③重放时报告"哪一段用什么口径"。
    """
    if not os.path.exists(path):
        return {'ok': False, 'entries': 0, 'problems': ['文件不存在']}
    problems = []
    prev = None
    entries = read_entries(path)
    segments = {}
    for i, e in enumerate(entries, 1):
        seq = e.get('seq')
        mode = 'insertion' if (isinstance(seq, int) and seq <= legacy_till) else 'sorted'
        segments[mode] = segments.get(mode, 0) + 1
        if seq != i:
            problems.append('第%d行 seq=%r 不连续' % (i, seq))
        if e.get('prev_hash') != prev:
            problems.append('第%d行 prev_hash 不接上一条' % i)
        want = digest({k: v for k, v in e.items() if k != 'entry_hash'}, mode=mode)
        if e.get('entry_hash') != want:
            problems.append('第%d行 entry_hash 重算不符（mode=%s）' % (i, mode))
        prev = e.get('entry_hash')
    return {'ok': not problems, 'entries': len(entries), 'problems': problems,
            'head': prev, 'legacy_till': legacy_till, 'segments': segments,
            'note': '混合口径分段校验；legacy_till 由调用方显式给，不猜测'}


def self_test():
    """分段校验自测 + 阴性对照。"""
    import tempfile
    results = []

    def ck(name, cond, detail=''):
        results.append((name, bool(cond)))
        print(('PASS ' if cond else 'FAIL ') + name + ((' | ' + detail) if detail else ''))

    tmp = tempfile.mkdtemp(prefix='chain_seg_')
    path = os.path.join(tmp, 'mixed.jsonl')
    try:
        # 造 3 条 insertion 口径 + 2 条 sorted 口径的混合账（模拟口径迁移）
        prev = None
        for seq in range(1, 6):
            mode = 'insertion' if seq <= 3 else 'sorted'
            # 故意用不同键插入序，制造两种口径的差异
            payload = {'z_field': 'v%d' % seq, 'a_field': seq, 'm': {'y': 2, 'b': 1}}
            e = dict(payload)
            e['seq'] = seq
            e['prev_hash'] = prev
            e['entry_hash'] = digest({k: v for k, v in e.items() if k != 'entry_hash'}, mode=mode)
            with io.open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(e, ensure_ascii=False, sort_keys=False) + '\n')
            prev = e['entry_hash']

        seg = verify_chain_segmented(path, legacy_till=3)
        ck('混合口径分段校验全绿', seg['ok'], str(seg.get('segments')))
        ck('分段计数正确', seg['segments'] == {'insertion': 3, 'sorted': 2}, str(seg['segments']))

        # 阴性对照：纯 sorted 校验旧账 → 应失败
        pure = verify_chain(path)
        ck('阴性对照：纯sorted校验旧账失败（假差额复现）', not pure['ok'],
           '%d条问题' % len(pure['problems']))

        # 用错误的 legacy_till（0）→ 应失败
        wrong = verify_chain_segmented(path, legacy_till=0)
        ck('错误legacy_till=0检出', not wrong['ok'])

        # 篡改检测仍有效
        lines = io.open(path, encoding='utf-8').read().splitlines()
        t = json.loads(lines[1])
        t['a_field'] = 999
        lines[1] = json.dumps(t, ensure_ascii=False, sort_keys=False)
        io.open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
        tampered = verify_chain_segmented(path, legacy_till=3)
        ck('分段校验下篡改仍检出', not tampered['ok'])
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    p = sum(1 for _, ok in results if ok)
    print('\nchain segmented self-test: %d/%d PASS' % (p, len(results)))
    return 0 if p == len(results) else 1


if __name__ == '__main__':
    import sys
    sys.exit(self_test())
