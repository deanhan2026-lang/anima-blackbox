# -*- coding: utf-8 -*-
"""对照器 · 言行差额 → DVEF 判定单元（2026-11）

核心设计：**对照器不输出自然语言报告，输出可复算的判定单元**。
这样外部复算器本来就能重放它——重放差额 = 重放对照逻辑，
不必另造"验证对照器的工具"（避免无限递归）。

判定单元结构（与 DVEF-001 v0.4 同构）：
  inputs    = A/B 两账事件数组 + inputs_digest
  params    = {rule_id, rule_version, spec_version, scope_policy} + params_digest
              —— 封闭枚举，不含时间戳/随机数（DVEF §6 硬约束）
  output    = {verdict, reason_code, score, diagnostics} + output_digest
              —— verdict ∈ {MATCH, MISMATCH, INCONCLUSIVE}
              —— INCONCLUSIVE 时 score=0 且 reason_code 必须存在（DVEF §7.1）
              —— diagnostics 不参与 output_digest（DVEF §7.1 约定）

三态语义：
  MATCH       两账言行一致（差额 score=0）
  MISMATCH    检出言行差额（score=差额条数 > 0）
  INCONCLUSIVE 证据不足（缺账 / 链破损 / scope 不可核）——不可伪装成 MATCH

对照逻辑（纯函数，外部可独立重放）：
  1. 按 scope_ref 关联 A/B 事件（A账 intent/self_report ↔ B账 action/consequence）
  2. 三类差额：SAID_NOT_DONE / DONE_NOT_SAID / PARAM_OUT_OF_SCOPE
  3. 任一账缺失 → INCONCLUSIVE（不许用"空账=无差额"蒙混）
"""
import chain

HERE = __file__
RULE_ID = 'R-BLACKBOX-RECONCILE'
RULE_VERSION = '0.1'
SPEC_VERSION = '0.1'
VERDICTS = ('MATCH', 'MISMATCH', 'INCONCLUSIVE')


def _scope_of(ev):
    return ev.get('scope_ref')


def _intent_keys(claims):
    """A账中'声称要做'的事件：channel=intent，用 content.action_key 做关联键。"""
    return {c.get('content_digest'): c for c in claims if c.get('channel') == 'intent'}


def reconcile(claim_events, exec_events, scope_registry=None):
    """纯函数对照。返回 (input_obj, params_obj, output_obj) 三元组（未加摘要）。

    纯函数 = 同输入必同输出，外部可用同一函数重放。
    """
    scope_registry = scope_registry or {}

    # 证据完整性闸：任一账为空 → INCONCLUSIVE（不许空账=无差额）
    if not claim_events or not exec_events:
        out = {'verdict': 'INCONCLUSIVE', 'score': 0,
               'reason_code': 'E-MISSING-LEDGER',
               'diagnostics': {'claims': len(claim_events), 'executions': len(exec_events)}}
        return {'claims': claim_events, 'executions': exec_events}, _params(), out

    # scope 可核性闸：B账引用的 scope 必须都在注册表且未失效
    bad_scope = [e.get('scope_ref') for e in exec_events
                 if e.get('scope_ref') not in scope_registry]
    if bad_scope:
        out = {'verdict': 'INCONCLUSIVE', 'score': 0,
               'reason_code': 'E-SCOPE-UNRESOLVABLE',
               'diagnostics': {'bad_scope_refs': sorted(set(bad_scope))}}
        return {'claims': claim_events, 'executions': exec_events}, _params(), out

    # 关联：用 (scope_ref, content_digest) 匹配"声称"与"执行"
    claim_index = {}
    for c in claim_events:
        if c.get('channel') == 'intent':
            k = (c.get('scope_ref'), c.get('content_digest'))
            claim_index[k] = c
    exec_index = {}
    for e in exec_events:
        if e.get('channel') == 'action':
            k = (e.get('scope_ref'), e.get('content_digest'))
            exec_index[k] = e

    gaps = []
    # 差额四级标注：
    #   MISMATCH_SET / MISMATCH_SCOPE / MISMATCH_CONSEQUENCE_STRUCTURED = 强可复算，进 output 判定位
    #   MISMATCH_CONSEQUENCE_NARRATIVE = 弱可复算，只能进 INCONCLUSIVE + reason_code
    # 1) 说了没做 / 2) 做了没说 —— 集合差，强可复算
    for k in claim_index:
        if k not in exec_index:
            gaps.append({'type': 'MISMATCH_SET', 'subtype': 'SAID_NOT_DONE',
                         'strength': 'STRONG', 'scope_ref': k[0], 'digest': k[1]})
    for k in exec_index:
        if k not in claim_index:
            gaps.append({'type': 'MISMATCH_SET', 'subtype': 'DONE_NOT_SAID',
                         'strength': 'STRONG', 'scope_ref': k[0], 'digest': k[1]})
    # 3) 参数越界 —— 结构化边界，强可复算
    for e in exec_events:
        if e.get('channel') == 'action':
            boundary = scope_registry.get(e.get('scope_ref'), {})
            allowed = boundary.get('authority_level')
            actual = e.get('authority_level')
            if allowed and actual and _level_rank(actual) > _level_rank(allowed):
                gaps.append({'type': 'MISMATCH_SCOPE', 'subtype': 'PARAM_OUT_OF_SCOPE',
                             'strength': 'STRONG', 'scope_ref': e.get('scope_ref'),
                             'allowed': allowed, 'actual': actual})
    # 4) 后果不符 —— 按证据形态分级（接缝三核心）
    for e in exec_events:
        if e.get('channel') == 'consequence' and e.get('outcome') == 'failure':
            structured = e.get('consequence_structured')  # 结构化后果对象（如 artifacts_changed/checksum）
            if structured is not None:
                gaps.append({'type': 'MISMATCH_CONSEQUENCE_STRUCTURED',
                             'subtype': 'CONSEQUENCE_MISMATCH', 'strength': 'STRONG',
                             'scope_ref': e.get('scope_ref'), 'digest': e.get('content_digest')})
            else:
                # 只有自然语言叙述 → 弱可复算，不进强判定位
                gaps.append({'type': 'MISMATCH_CONSEQUENCE_NARRATIVE',
                             'subtype': 'CONSEQUENCE_NARRATIVE', 'strength': 'WEAK',
                             'scope_ref': e.get('scope_ref'), 'digest': e.get('content_digest')})

    gaps.sort(key=lambda g: (g['type'], g.get('subtype', ''), g.get('scope_ref', ''), g.get('digest', '')))
    strong = [g for g in gaps if g['strength'] == 'STRONG']
    weak = [g for g in gaps if g['strength'] == 'WEAK']

    # evidence_strength 进 inputs（进而进 inputs_digest）——
    # 防止"强/弱证据上达成同样 verdict"被外部复算器误认为同等可信（一致造假的另一形态）
    evidence = {'strong_gaps': len(strong), 'weak_gaps': len(weak),
                'weak_only': bool(weak and not strong)}

    if weak and not strong:
        # 只有弱证据 → INCONCLUSIVE，不判 MISMATCH
        out = {'verdict': 'INCONCLUSIVE', 'score': 0,
               'reason_code': 'WEAK_SEMANTIC_EVIDENCE',
               'diagnostics': {'gaps': gaps, 'evidence': evidence}}
    elif strong:
        out = {'verdict': 'MISMATCH', 'score': len(strong),
               'reason_code': None,
               'diagnostics': {'gaps': gaps, 'evidence': evidence,
                               'note': '弱证据项已计入 diagnostics 但不参与 score'}}
    else:
        out = {'verdict': 'MATCH', 'score': 0,
               'reason_code': None, 'diagnostics': {'gaps': [], 'evidence': evidence}}
    inputs = {'claims': claim_events, 'executions': exec_events, 'evidence_strength': evidence}
    return inputs, _params(), out


def _level_rank(level):
    return {'reference': 0, 'authorize': 1}.get(level, -1)


def _params():
    # 不含时间戳/随机数/会话ID——DVEF §6 硬约束
    return {'rule_id': RULE_ID, 'rule_version': RULE_VERSION,
            'spec_version': SPEC_VERSION, 'scope_policy': 'registry_ref_only'}


def seal(input_obj, params_obj, output_obj):
    """把三元组封成带摘要的 DVEF 判定单元（output.diagnostics 不进摘要）。"""
    verdict = output_obj['verdict']
    if verdict not in VERDICTS:
        raise ValueError('E-VERDICT-INVALID: %r' % verdict)
    score = output_obj['score']
    if verdict == 'INCONCLUSIVE' and score != 0:
        raise ValueError('E-INCONCLUSIVE-SCORE: INCONCLUSIVE 时 score 必须为 0')
    if verdict == 'INCONCLUSIVE' and not output_obj.get('reason_code'):
        raise ValueError('E-REASON-REQUIRED: INCONCLUSIVE 必须带 reason_code')
    if verdict == 'MATCH' and score != 0:
        raise ValueError('E-MATCH-SCORE: MATCH 时 score 必须为 0')
    if verdict == 'MISMATCH' and score <= 0:
        raise ValueError('E-MISMATCH-SCORE: MISMATCH 时 score 必须 > 0')

    out_digest_src = {k: v for k, v in output_obj.items() if k != 'diagnostics'}
    return {
        'spec_version': SPEC_VERSION,
        'rule_id': params_obj['rule_id'],
        'rule_version': params_obj['rule_version'],
        'inputs': input_obj,
        'inputs_digest': chain.digest(input_obj),
        'params': params_obj,
        'params_digest': chain.digest(params_obj),
        'output': output_obj,
        'output_digest': chain.digest(out_digest_src),
    }


def replay(unit):
    """外部独立重放：只吃判定单元，重跑对照逻辑，比对三类摘要与 verdict/score。

    这是 §3.3 的'对照器本身可复算'——外部不需要我们的服务，拿单元就能算。
    """
    problems = []
    if chain.digest(unit.get('inputs')) != unit.get('inputs_digest'):
        problems.append('E-INPUTS-DIGEST')
    params = unit.get('params', {})
    if chain.digest(params) != unit.get('params_digest'):
        problems.append('E-PARAMS-DIGEST')
    out = unit.get('output', {})
    out_src = {k: v for k, v in out.items() if k != 'diagnostics'}
    if chain.digest(out_src) != unit.get('output_digest'):
        problems.append('E-OUTPUT-DIGEST')
    # 重跑逻辑（scope_registry 从 inputs 里取不到则视为不可核 → INCONCLUSIVE）
    # 外部重放时若没有原始 scope 注册表，只做摘要级一致性 + 三态合法性校验；
    # 完整语义重放需外部持有同版 scope_registry（有限深度边界，见 README）。
    recalc = {'verdict': out.get('verdict'), 'score': out.get('score'),
              'reason_code': out.get('reason_code')}
    if recalc['verdict'] not in VERDICTS:
        problems.append('E-VERDICT-INVALID')
    if recalc['verdict'] == 'INCONCLUSIVE' and not recalc['reason_code']:
        problems.append('E-REASON-MISSING')
    return {'ok': not problems, 'problems': problems,
            'replayed': recalc, 'level': 'CL1-digest-consistency'}
