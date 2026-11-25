# -*- coding: utf-8 -*-
"""forge 防护扩展 · 区分 tamper 与 forge（2026-11-25）

来源：恒《史官锐评·051》活体反例——15 封邮件正文在、.flag/.done 齐、append-only
成立，但"已读"由 reflex 脚本代签，"恒读过"这一认知事件从未发生。

结论：append-only 防 tamper（篡改过去），不防 forge（伪造曾经）。
本模块提供两层防线，均为 sandbox 参考实现、零生产改动：

  ① scope 注册表冻结（FrozenScopeRegistry）
     - 构造时深拷贝；外部再改原 dict 不影响账本判定
     - 判定只能引用快照，不能事后放宽（Memory Trap 的攻击路径）
  ② 认知事件声明（attest）
     - A 账写入附结论摘要（非模板）+ 签署者类型（self/delegated）
     - delegated 声明进账但标弱证据，不进强判定位——代签不能冒充真实声称

设计纪律：不修改 chain.py / ledgers.py / reconciler.py 任何既有权威文件；
靠"冻结快照 + 判定期过滤"组合接入。原 24/24 自测不受影响。
"""
import copy
import json

SELF = 'self'
DELEGATED = 'delegated'
SIGNERS = (SELF, DELEGATED)


class ForgeError(Exception):
    pass


def frozen_snapshot(registry):
    """冻结 scope 注册表快照：深拷贝 + 转不可变 JSON 串存档。

    返回 (snapshot_dict, fingerprint)。判定必须用 snapshot，不得用调用方原件。
    """
    if not isinstance(registry, dict):
        raise ForgeError('E-REGISTRY-TYPE: 注册表须为 dict')
    snap = copy.deepcopy(registry)
    canonical = json.dumps(snap, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return snap, canonical


def scope_still_valid(snapshot, scope_id, authority_level):
    """判定时核验：scope 存在 + 授权级别未超出冻结快照。

    Memory Trap 的攻击路径不是改内容，是悄悄放宽作用域。所以比较的是
    「写入时的级别」对「冻结快照里的上限」，不是对调用方当下给的注册表。
    """
    rule = snapshot.get(scope_id)
    if rule is None:
        return False, 'E-SCOPE-UNRESOLVABLE'
    allowed = rule.get('authority_level')
    rank = {'reference': 0, 'authorize': 1}
    if allowed in rank and authority_level in rank:
        if rank[authority_level] > rank[allowed]:
            return False, 'E-SCOPE-ESCALATED'
    return True, None


def attest(content_digest, conclusion, signer=SELF, note=None):
    """构造 A 账认知事件声明。

    conclusion 必须是写入者自己的结论摘要（非模板、非脚本生成）——
    这是"实际发生的认知事件"与"代理代签"的分界线。
    """
    if signer not in SIGNERS:
        raise ForgeError('E-SIGNER-INVALID: %r' % signer)
    if not conclusion or not str(conclusion).strip():
        raise ForgeError('E-CONCLUSION-REQUIRED: 认知事件须带写入者结论摘要')
    a = {'content_digest': content_digest,
         'conclusion': str(conclusion).strip(),
         'signer': signer}
    if note:
        a['note'] = str(note)
    return a


def is_strong_assertion(assertion):
    """是否可作为强判定的『真实声称』。

    self + 有结论摘要 = 强；delegated = 弱（进账但不冒充主体认知）。
    """
    if not isinstance(assertion, dict):
        return False
    return (assertion.get('signer') == SELF
            and bool(str(assertion.get('conclusion', '')).strip()))


def split_claims(claim_events):
    """把 A 账事件按认知强度分组。

    返回 (strong, delegated)。对照器只应把 strong 计入 SAID_NOT_DONE 等强差额；
    delegated 进账、可被外部审计，但不作为"主体声称过"的证据。
    """
    strong, delegated = [], []
    for e in claim_events:
        (strong if is_strong_assertion(e.get('assertion')) else delegated).append(e)
    return strong, delegated


def self_test():
    results = []
    def ck(name, cond, detail=''):
        results.append((name, bool(cond)))
        print(('PASS ' if cond else 'FAIL ') + name + ((' | ' + detail) if detail else ''))

    # ① 冻结快照：外部篡改原注册表不影响快照
    reg = {'scope_A': {'authority_level': 'reference'}}
    snap, fp = frozen_snapshot(reg)
    reg['scope_A']['authority_level'] = 'authorize'      # 事后放宽（攻击）
    reg['scope_NEW'] = {'authority_level': 'authorize'}  # 事后新增（攻击）
    # 用写入时的级别 reference 核：冻结快照里仍是 reference → 合法
    ok1, why1 = scope_still_valid(snap, 'scope_A', 'reference')
    ck('scope 冻结：外部放宽原注册表不影响快照', ok1 and why1 is None, str(why1))
    # 写入时若用了 authorize（超出冻结上限）→ 检出扩张
    ok2, why2 = scope_still_valid(snap, 'scope_A', 'authorize')
    ck('scope 冻结：事后越权级别被检出', (not ok2) and why2 == 'E-SCOPE-ESCALATED', str(why2))
    ck('scope 冻结：事后新增 scope 不可见', not scope_still_valid(snap, 'scope_NEW', 'reference')[0])
    ck('scope 冻结：快照指纹稳定', len(fp) > 0 and fp == json.dumps(
        {'scope_A': {'authority_level': 'reference'}}, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False))

    # ② 认知事件声明
    good = attest('a' * 64, '我确认本次只读取、不写入', signer=SELF)
    ck('attest：self + 结论摘要 = 强声明', is_strong_assertion(good))
    bad = attest('b' * 64, '脚本代签', signer=DELEGATED)
    ck('attest：delegated = 弱声明（不冒充真实认知）', not is_strong_assertion(bad))
    try:
        attest('c' * 64, '', signer=SELF)
        empty_rejected = False
    except ForgeError:
        empty_rejected = True
    ck('attest：空结论摘要被拒', empty_rejected)
    try:
        attest('d' * 64, 'x', signer='robot')
        signer_rejected = False
    except ForgeError:
        signer_rejected = True
    ck('attest：未知签署者类型被拒', signer_rejected)

    # ③ 代签场景：复现恒的 15 封——记录完整但认知事件是代签
    claims = [{'channel': 'intent', 'assertion': good, 'content_digest': 'a' * 64},
              {'channel': 'intent', 'assertion': bad, 'content_digest': 'b' * 64},
              {'channel': 'intent', 'content_digest': 'e' * 64}]  # 无声明（旧账）
    strong, delegated = split_claims(claims)
    ck('代签场景：强/弱/无声明正确分组', len(strong) == 1 and len(delegated) == 2,
       'strong=%d delegated=%d' % (len(strong), len(delegated)))

    p = sum(1 for _, ok in results if ok)
    print('\nattestation self-test: %d/%d PASS' % (p, len(results)))
    return 0 if p == len(results) else 1


if __name__ == '__main__':
    import sys
    sys.exit(self_test())
