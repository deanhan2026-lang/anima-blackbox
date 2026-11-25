# -*- coding: utf-8 -*-
"""两账写入 · A账(声明账) / B账(执行账) 物理分离（2026-11）

核心设计：主体自述声明（A 账）与实际执行（B 账）分开保管、互不改写。

三进程模型（sandbox 进程级模拟；账号级隔离见 README「边界」）：
  - 主体 / A_WRITER : 写 A 账（主体自述 plan/intent/self_report）——写完不可改，写入时由主体控制；
  - B_SIDECAR       : 写 B 账，独立伴生进程。主体只能发消息，不能直接写文件。
  - RECONCILER      : 下游消费者（非黑匣子组件），对两账只读，连 append 都没有。

本模块用"调用方必须持 writer_token"模拟进程级权限分离：
  - 写 A 账须 token ∈ {A_WRITER, ACTOR}；写 B 账须 token == B_SIDECAR；
  - RECONCILER 对两账无写权限；ACTOR 对 B 账无写权限。
**A 账独立性弱于 B 账**：靠"写完不可改"（append-only+链+锚定），不保证"写下时为真"——见 README「边界」。
真实生产建议用不同系统账号/文件权限，此处为 sandbox 参考实现，零生产改动。

字段集（封闭枚举，缺一拒写）：
  channel ∈ {intent, self_report, action, consequence}
  scope_ref 只能引用既有授权边界（REGISTRY），不能由事件创建
  outcome ∈ {success, failure, inconclusive}；inconclusive 必须带 reason_code
"""
import io
import json
import os

import chain

HERE = os.path.dirname(os.path.abspath(__file__))

# 三进程身份（sandbox 模拟；真实生产为不同系统账号）
A_WRITER = 'A_WRITER'
B_SIDECAR = 'B_SIDECAR'
# A 账记"主体自述声明"，必须由主体写——
# 独立性不靠"不可写"，靠"写完不可改"（append-only + 链 + 外部锚定）。
ACTOR = 'ACTOR'          # 被审计主体——可写 A账（自述声明），对 B账只读
RECONCILER = 'RECONCILER'  # 下游消费者（非组件）——对两账只读，连 append 都没有
VALID_WRITERS = {A_WRITER: 'claim', ACTOR: 'claim', B_SIDECAR: 'execution'}

CHANNELS = ('intent', 'self_report', 'action', 'consequence')
OUTCOMES = ('success', 'failure', 'inconclusive')
SPEC_VERSION = '0.1'


class PermError(Exception):
    pass


class FieldError(Exception):
    pass


class LedgerSet:
    """两账容器。账文件路径分离，写入权限按 writer_token 隔离。"""

    def __init__(self, dirpath, scope_registry=None):
        self.dirpath = dirpath
        os.makedirs(dirpath, exist_ok=True)
        self.claim_path = os.path.join(dirpath, 'claim_log.jsonl')
        self.exec_path = os.path.join(dirpath, 'execution_log.jsonl')
        self.scope_registry = scope_registry or {}   # scope_id -> 授权边界对象（只引用不创建）

    def _check_perm(self, writer_token, want):
        if VALID_WRITERS.get(writer_token) != want:
            raise PermError('E-PERM-DENIED: %s 无权写%s账' % (writer_token, want))

    def _validate(self, ev):
        # 必填字段
        for k in ('channel', 'occurred_at', 'actor_ref', 'content', 'scope_ref',
                  'rule_version', 'outcome'):
            if k not in ev or ev[k] in (None, ''):
                raise FieldError('E-FIELD-MISSING: %s' % k)
        if ev['channel'] not in CHANNELS:
            raise FieldError('E-CHANNEL-INVALID: %r' % ev['channel'])
        if ev['outcome'] not in OUTCOMES:
            raise FieldError('E-OUTCOME-INVALID: %r' % ev['outcome'])
        if ev['outcome'] == 'inconclusive' and not ev.get('reason_code'):
            raise FieldError('E-REASON-REQUIRED: inconclusive 必须带 reason_code')
        # scope_ref 只能引用既有边界，不能由事件创建
        if ev['scope_ref'] not in self.scope_registry:
            raise FieldError('E-SCOPE-REF-UNKNOWN: %r 不在授权边界注册表' % ev['scope_ref'])
        # content 必须是可 canonical 化的对象（不收自由文本）
        try:
            chain.canon(ev['content'])
        except Exception:
            raise FieldError('E-CONTENT-NOT-CANONICAL')

    def _write(self, path, writer_token, want, ev, now):
        self._check_perm(writer_token, want)
        e = dict(ev)
        e['spec_version'] = SPEC_VERSION
        e['occurred_at'] = now               # 时间由调用方显式给，模块不取时钟
        e['writer'] = writer_token
        self._validate(e)
        e['content_digest'] = chain.digest(e['content'])
        # 标准化落盘字段（去掉 content 本体，只留摘要——外部锚定不发明细）
        payload = {k: e[k] for k in ('channel', 'occurred_at', 'actor_ref', 'scope_ref',
                                     'rule_version', 'spec_version', 'outcome',
                                     'content_digest', 'writer')}
        # authority_level 提升到顶层（结构化字段，纳入链摘要防篡改）——
        # scope_ref 绑定授权边界，执行级别必须是可判别的顶层字段。
        # 调用方可在 content 里带 authority_level，此处提取；显式顶层参数优先。
        lvl = e.get('authority_level') or (
            e['content'].get('authority_level') if isinstance(e['content'], dict) else None)
        if lvl:
            if lvl not in ('reference', 'authorize'):
                raise FieldError('E-AUTH-LEVEL-INVALID: %r' % lvl)
            payload['authority_level'] = lvl
        if 'reason_code' in e:
            payload['reason_code'] = e['reason_code']
        # 可选：consequence 渠道的后果表述摘要（后果反转判据的输入）
        if 'consequence_digest' in e:
            payload['consequence_digest'] = e['consequence_digest']
        return chain.append_entry(path, payload)

    def write_claim(self, ev, now, writer_token=A_WRITER):
        return self._write(self.claim_path, writer_token, 'claim', ev, now)

    def write_execution(self, ev, now, writer_token=B_SIDECAR):
        return self._write(self.exec_path, writer_token, 'execution', ev, now)

    def read_claims(self):
        return chain.read_entries(self.claim_path)

    def read_executions(self):
        return chain.read_entries(self.exec_path)

    def head_hashes(self):
        """两账链头——供 L3 锚定（只锚摘要不发明细）。"""
        c = chain.read_entries(self.claim_path)
        e = chain.read_entries(self.exec_path)
        return {
            'claim_head': c[-1]['entry_hash'] if c else None,
            'execution_head': e[-1]['entry_hash'] if e else None,
        }
