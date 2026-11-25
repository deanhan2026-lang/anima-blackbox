# -*- coding: utf-8 -*-
"""全套自测 · 逐条验收 README 所列判据。

判据（README「验收判据」）：
  ① 言行差额三类场景各能被检出
  ② Sidecar 被 kill → Watchdog 报警并标记日志段缺失
  ③ 事件流能机械转成 DVEF inputs/params
  ④ 三进程不同账号、Reconciler 对两账无写权限
  ⑤ 差额报告即 DVEF 判定单元，外部复算器能重放、伪造差额=0 的反向样本被检出
附加：append-only 链完整性、篡改检测、空账不伪装无差额、字段封闭枚举、scope 只引用不创建
"""
import io
import json
import os
import shutil
import sys
import tempfile

import chain
import ledgers
import reconciler
import watchdog

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPE = {
    'scope_task_A': {'authority_level': 'reference', 'task': '清理旧文件'},
    'scope_task_B': {'authority_level': 'authorize', 'task': '支付审批'},
}
T0 = '2026-11T03:00:00Z'
T1 = '2026-11T03:05:00Z'
T2 = '2026-11T03:10:00Z'

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(('PASS ' if cond else 'FAIL ') + name + ((' | ' + detail) if detail else ''))


def ev(channel, content, scope='scope_task_A', outcome='success', **extra):
    d = {'channel': channel, 'actor_ref': 'actor@engine-X+car-Y@v1',
         'content': content, 'scope_ref': scope, 'rule_version': '0.1',
         'outcome': outcome}
    d.update(extra)
    return d


def run():
    tmp = tempfile.mkdtemp(prefix='bb_selftest_')
    try:
        L = ledgers.LedgerSet(os.path.join(tmp, 'ledgers'), scope_registry=SCOPE)

        # ── ④ 权限分离（A 账主体可写、B 账主体不可写）──
        denied = 0
        for tok, fn in ((ledgers.ACTOR, L.write_execution),
                        (ledgers.RECONCILER, L.write_claim), (ledgers.RECONCILER, L.write_execution),
                        (ledgers.A_WRITER, L.write_execution), (ledgers.B_SIDECAR, L.write_claim)):
            try:
                fn(ev('intent', {'x': 1}), T0, writer_token=tok)
            except ledgers.PermError:
                denied += 1
        check('④进程级权限分离（主体写A账/只能写A账；对照器只读；跨账写入被拒）', denied == 5,
              '拒绝 %d/5' % denied)
        check('④主体可写A账（自述声明）',
              bool(L.write_claim(ev('intent', {'action_key': 'self_declared'}), T0,
                                 writer_token=ledgers.ACTOR)))
        check('④合法写入者仍可写各自账',
              bool(L.write_claim(ev('intent', {'action_key': 'cleanup'}), T0)) and
              bool(L.write_execution(ev('action', {'action_key': 'cleanup'}), T0)))

        # ── 字段封闭枚举 + scope 只引用 ──────────────────────
        bad_channel = 0
        try:
            L.write_claim(ev('thought', {'x': 1}), T0)
        except ledgers.FieldError:
            bad_channel = 1
        check('附加·channel 封闭枚举（thought 被拒）', bad_channel == 1)
        bad_scope = 0
        try:
            L.write_claim(ev('intent', {'x': 1}, scope='scope_FABRICATED'), T0)
        except ledgers.FieldError:
            bad_scope = 1
        check('附加·scope_ref 只引用不创建（伪造 scope 被拒）', bad_scope == 1)
        bad_inc = 0
        try:
            L.write_claim(ev('intent', {'x': 1}, outcome='inconclusive'), T0)
        except ledgers.FieldError:
            bad_inc = 1
        check('附加·inconclusive 必须带 reason_code', bad_inc == 1)

        # ── ① 三类差额场景 ──────────────────────────────────
        def fresh_ledger(name):
            return ledgers.LedgerSet(os.path.join(tmp, name), scope_registry=SCOPE)

        # 场景1：说了没做
        L1 = fresh_ledger('case1')
        L1.write_claim(ev('intent', {'action_key': 'claim_only'}), T0)
        L1.write_execution(ev('action', {'action_key': 'unrelated'}), T1)
        u1 = reconciler.seal(*reconciler.reconcile(L1.read_claims(), L1.read_executions(), SCOPE))
        g1 = [g.get('subtype', g['type']) for g in u1['output']['diagnostics']['gaps']]
        check('①-1 说了没做 → SAID_NOT_DONE 检出', u1['output']['verdict'] == 'MISMATCH'
              and 'SAID_NOT_DONE' in g1, str(g1))

        # 场景2：做了没说
        L2 = fresh_ledger('case2')
        L2.write_claim(ev('intent', {'action_key': 'other'}), T0)
        L2.write_execution(ev('action', {'action_key': 'secret_action'}), T1)
        u2 = reconciler.seal(*reconciler.reconcile(L2.read_claims(), L2.read_executions(), SCOPE))
        g2 = [g.get('subtype', g['type']) for g in u2['output']['diagnostics']['gaps']]
        check('①-2 做了没说 → DONE_NOT_SAID 检出', u2['output']['verdict'] == 'MISMATCH'
              and 'DONE_NOT_SAID' in g2, str(g2))

        # 场景3：参数越界（执行用 authorize，授权只给 reference）
        L3 = fresh_ledger('case3')
        L3.write_claim(ev('intent', {'action_key': 'cleanup'}, scope='scope_task_A'), T0)
        L3.write_execution(ev('action', {'action_key': 'cleanup'}, scope='scope_task_A',
                              authority_level='authorize'), T1)
        u3 = reconciler.seal(*reconciler.reconcile(L3.read_claims(), L3.read_executions(), SCOPE))
        g3 = [g.get('subtype', g['type']) for g in u3['output']['diagnostics']['gaps']]
        check('①-3 参数越界 → PARAM_OUT_OF_SCOPE 检出',
              'PARAM_OUT_OF_SCOPE' in g3, str(g3))

        # 接缝三：弱可复算（自然语言后果）不能进强判定位
        LW = fresh_ledger('weak_evidence')
        LW.write_claim(ev('intent', {'action_key': 'same'}), T0)
        LW.write_execution(ev('action', {'action_key': 'same'}), T1)
        LW.write_execution(ev('consequence', {'narrative': '文件已成功整理'},
                              outcome='failure', reason_code='narrative_only'), T2)
        uw = reconciler.seal(*reconciler.reconcile(LW.read_claims(), LW.read_executions(), SCOPE))
        check('接缝三·仅自然语言后果差异 → INCONCLUSIVE 不判 MISMATCH',
              uw['output']['verdict'] == 'INCONCLUSIVE'
              and uw['output']['reason_code'] == 'WEAK_SEMANTIC_EVIDENCE', str(uw['output']['verdict']))
        check('接缝三·evidence_strength 进 inputs_digest',
              'evidence_strength' in uw['inputs']
              and uw['inputs_digest'] == chain.digest(uw['inputs']))
        # narrative 不进落盘摘要字段：账本记录只应含 content_digest，不含 content 本体
        exec_entries = LW.read_executions()
        cons_entry = [e for e in exec_entries if e.get('channel') == 'consequence'][0]
        check('接缝三·narrative 不进落盘摘要（只留 content_digest）',
              'content' not in cons_entry and 'narrative' not in cons_entry
              and bool(cons_entry.get('content_digest')),
              '落盘字段: %s' % ','.join(sorted(cons_entry.keys())))

        # 言行一致 → MATCH
        L0 = fresh_ledger('case0')
        L0.write_claim(ev('intent', {'action_key': 'same'}), T0)
        L0.write_execution(ev('action', {'action_key': 'same'}), T1)
        u0 = reconciler.seal(*reconciler.reconcile(L0.read_claims(), L0.read_executions(), SCOPE))
        check('①-4 言行一致 → MATCH score=0', u0['output']['verdict'] == 'MATCH'
              and u0['output']['score'] == 0)

        # 空账不伪装无差额
        uempty = reconciler.seal(*reconciler.reconcile([], [], SCOPE))
        check('附加·空账 → INCONCLUSIVE（不伪装无差额）',
              uempty['output']['verdict'] == 'INCONCLUSIVE'
              and uempty['output']['reason_code'] == 'E-MISSING-LEDGER')

        # ── ③ DVEF inputs/params 机械转换 ──────────────────
        check('③事件流机械转 DVEF inputs/params',
              'claims' in u1['inputs'] and 'executions' in u1['inputs']
              and u1['params']['rule_id'] == 'R-BLACKBOX-RECONCILE'
              and u1['params']['rule_version'] == '0.1'
              and 'occurred_at' not in u1['params'],
              'params 无时间戳/随机数')

        # ── ⑤ 判定单元可重放 + 反向样本 ────────────────────
        r_ok = reconciler.replay(u1)
        check('⑤外部重放判定单元摘要一致', r_ok['ok'], str(r_ok['problems']))
        forged = json.loads(json.dumps(u1))
        forged['output']['score'] = 0
        forged['output']['verdict'] = 'MATCH'
        # 改 output 但保留旧 output_digest → 重放必检出
        r_forged = reconciler.replay(forged)
        check('⑤伪造"差额=0"反向样本被检出',
              not r_forged['ok'] and 'E-OUTPUT-DIGEST' in r_forged['problems'],
              str(r_forged['problems']))

        # ── ② Watchdog 掉线检测 ────────────────────────────
        LW = fresh_ledger('watchdog')
        LW.write_execution(ev('action', {'action_key': 'a'}), T0)
        watchdog.write_heartbeat(os.path.join(tmp, 'watchdog'), 1, T0, ledgers.B_SIDECAR)
        ok_report = watchdog.check(os.path.join(tmp, 'watchdog'), LW.exec_path, T1)
        check('②心跳与账本同步 → OK', ok_report['ok'])
        # 模拟 kill：心跳说写到 3，账本只有 1 → 段缺失
        watchdog.write_heartbeat(os.path.join(tmp, 'watchdog'), 3, T1, ledgers.B_SIDECAR)
        gap_report = watchdog.check(os.path.join(tmp, 'watchdog'), LW.exec_path, T2)
        check('②Sidecar 被 kill → 报警并标记段缺失',
              not gap_report['ok'] and gap_report['gap']['status'] == watchdog.MISSING
              and gap_report['gap']['missing_from'] == 2,
              str(gap_report['gap']))
        # 非 B_SIDECAR 不能写心跳
        hb_denied = 0
        try:
            watchdog.write_heartbeat(os.path.join(tmp, 'watchdog'), 9, T2, ledgers.ACTOR)
        except PermissionError:
            hb_denied = 1
        check('②主体不能伪造心跳', hb_denied == 1)

        # ── append-only 链完整性 + 篡改检测 ──────────────────
        for nm, p in (('A账', L0.claim_path), ('B账', L0.exec_path)):
            v = chain.verify_chain(p)
            check('附加·%s append-only 链有效' % nm, v['ok'], '%d条' % v['entries'])
        # 篡改中间一条
        with io.open(L0.claim_path, 'a', encoding='utf-8') as f:
            pass
        lines = io.open(L0.claim_path, encoding='utf-8').read().splitlines()
        tampered = json.loads(lines[0])
        tampered['channel'] = 'consequence'
        lines[0] = json.dumps(tampered, ensure_ascii=False, sort_keys=True)
        io.open(L0.claim_path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
        v_bad = chain.verify_chain(L0.claim_path)
        check('附加·篡改被链校验检出', not v_bad['ok']
              and any('entry_hash' in p for p in v_bad['problems']),
              str(v_bad['problems'][:1]))

        # ── 链头供外部锚定（只锚摘要）────────────────────────
        heads = L0.head_hashes()
        check('附加·两账链头可供 L3 锚定（摘要）',
              bool(heads['claim_head']) and bool(heads['execution_head'])
              and 'content' not in json.dumps(heads), str(heads)[:60])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    run()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print('\n' + '=' * 60)
    print('黑匣子自测：%d/%d PASS' % (passed, total))
    print('=' * 60)
    sys.exit(0 if passed == total else 1)
