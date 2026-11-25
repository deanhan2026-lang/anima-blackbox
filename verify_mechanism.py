# -*- coding: utf-8 -*-
"""写入端机械性检验器（2026-11-25）

第一性原理：黑匣子记录必须客观、机械、不留余量；引入另一个"会判断"的
主体＝引入可变量＝可靠性下降。B1 订正后判据的后半条——"sidecar 的 append
逻辑仍纯机械"——过去只有文字声明，本检验器把它变成可跑的判据。

机械性＝无裁量权。可检验的四个面：
  ① 确定性：同 payload 写两条独立账，产出逐字节一致（含 seq/prev/entry_hash）
  ② 字段封闭：payload 只含固定字段集，多一个字段都不接受（无"自由发挥"通道）
  ③ 不取时钟：时间由调用方显式给，同一 occurred_at 两次写结果一致
  ④ 无裁量分支：合法输入一律落盘、无跳过；reserved 字段一律拒收、无例外

**诚实边界（不许省）**：
  - 本检验器测的是【我方参考实现】chain.append_entry / ledgers._write 的机械性，
    不证明瞬的 sidecar.js 机械（那要看他的代码/实测）；
  - 机械性 ≠ 可信度：它只证明"规则固定无裁量"，不证明"规则正确"或"输入真实"；
  - 这是只读检验工具，不参与任何写入，不是新的判断主体——不违反第一性原理。
"""
import io
import json
import os
import shutil
import sys
import tempfile

import chain
import ledgers


SCOPE = {'scope_A': {'authority_level': 'reference'}}
T = '2026-11-25T03:00:00Z'


def self_test():
    results = []
    def ck(name, cond, detail=''):
        results.append((name, bool(cond)))
        print(('PASS ' if cond else 'FAIL ') + name + ((' | ' + detail) if detail else ''))

    tmp = tempfile.mkdtemp(prefix='mech_probe_')
    try:
        p1 = os.path.join(tmp, 'a.jsonl')
        p2 = os.path.join(tmp, 'b.jsonl')
        payload = {'channel': 'action', 'actor_ref': 'actor@engine@v1',
                   'scope_ref': 'scope_A', 'occurred_at': T,
                   'rule_version': '0.1', 'outcome': 'success',
                   'content_digest': 'c' * 64}

        # ① 确定性：同 payload → 逐字节一致的 entry
        e1 = chain.append_entry(p1, dict(payload))
        e2 = chain.append_entry(p2, dict(payload))
        line1 = io.open(p1, encoding='utf-8').read()
        line2 = io.open(p2, encoding='utf-8').read()
        ck('①确定性：同 payload 产出完全一致的 entry', e1 == e2, str(e1.get('seq')))
        ck('①确定性：落盘字节逐字相同（无随机/无时钟）', line1 == line2)

        # ② reserved 字段一律拒收（无"自己塞链字段"的裁量通道）
        reserved_ok = 0
        for k in ('seq', 'prev_hash', 'entry_hash'):
            bad = dict(payload)
            bad[k] = 'FABRICATED'
            try:
                chain.append_entry(os.path.join(tmp, 'r_%s.jsonl' % k), bad)
            except ValueError:
                reserved_ok += 1
        ck('②无例外：seq/prev_hash/entry_hash 三个 reserved 全拒收',
           reserved_ok == 3, '%d/3' % reserved_ok)

        # ③ 不取时钟：同一 occurred_at 两次写一致；不同 occurred_at 才有别
        e3 = chain.append_entry(p1, dict(payload))   # 第2条，同 occurred_at
        ck('③不取时钟：同 occurred_at 的第二条除链字段外一致',
           all(e3[k] == payload[k] for k in payload)
           and e3['seq'] == 2 and e3['prev_hash'] == e1['entry_hash'])
        payload_late = dict(payload, occurred_at='2026-11-25T09:00:00Z')
        e4 = chain.append_entry(os.path.join(tmp, 'late.jsonl'), payload_late)
        ck('③时间由调用方给：不同 occurred_at 产出不同 entry_hash',
           e4['entry_hash'] != e1['entry_hash'])

        # ④ 无裁量分支：N 条合法输入全部落盘、无跳过、字段数固定
        many = os.path.join(tmp, 'many.jsonl')
        n = 12
        for i in range(n):
            chain.append_entry(many, dict(payload, content_digest=('%064d' % i)))
        entries = chain.read_entries(many)
        field_sets = {tuple(sorted(e.keys())) for e in entries}
        ck('④无跳过：%d 条合法输入全部落盘' % n, len(entries) == n, '%d条' % len(entries))
        ck('④字段固定：所有 entry 字段集相同（无裁量增删）',
           len(field_sets) == 1, '%d种字段集' % len(field_sets))

        # ⑤ 封闭枚举：channel/outcome 非法一律拒（无"这个先不记/换个类"的余量）
        L = ledgers.LedgerSet(os.path.join(tmp, 'ledgers'), scope_registry=SCOPE)
        rejected = 0
        for ch, oc in (('thought', 'success'), ('intent', 'maybe'), ('intent', 'inconclusive')):
            ev = {'channel': ch, 'actor_ref': 'a@v', 'content': {'x': 1},
                  'scope_ref': 'scope_A', 'rule_version': '0.1', 'outcome': oc}
            try:
                L.write_claim(ev, T)
            except ledgers.FieldError:
                rejected += 1
        ck('⑤封闭枚举：非法 channel/outcome 全拒（inconclusive 无 reason 也拒）',
           rejected == 3, '%d/3' % rejected)

        # ⑥ 反向：机械记录仍可被篡改检出（机械 ≠ 无敌，但篡改必留痕）
        lines = io.open(p1, encoding='utf-8').read().splitlines()
        t = json.loads(lines[0])
        t['content_digest'] = 'f' * 64
        lines[0] = json.dumps(t, ensure_ascii=False, sort_keys=True)
        io.open(p1, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
        v = chain.verify_chain(p1)
        ck('⑥反向：篡改机械记录被链校验检出', not v['ok']
           and any('entry_hash' in p for p in v['problems']))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    p = sum(1 for _, ok in results if ok)
    print('\nverify_mechanism self-test: %d/%d PASS' % (p, len(results)))
    print('提醒：本检验只证明【我方参考实现】写入端机械，不证明任何外部节点机械；')
    print('      机械性 ≠ 可信度——它只证明规则固定，不证明输入真实或规则正确。')
    return 0 if p == len(results) else 1


if __name__ == '__main__':
    sys.exit(self_test())
