# -*- coding: utf-8 -*-
"""Watchdog · Sidecar 掉线检测与日志段缺失标记（2026-11）

模拟 Sidecar 被 kill → Watchdog 报警并标记该段日志缺失。

机制（sandbox 参考实现，纯只读观察）：
  - B_SIDECAR 每写一条记录，同时更新 heartbeat.json（带 seq + 时间，由调用方给）
  - Watchdog 检查：heartbeat 停止推进 / 心跳声明的 seq 与 B 账实际条数不符 → 报警
  - 报警输出结构化 gap 标记，供 Reconciler 在判定单元里标 INCONCLUSIVE

真实生产用独立守护进程 + 系统信号，此处零生产改动。
"""
import io
import json
import os

import chain

MISSING = 'MISSING'


def heartbeat_path(ledger_dir):
    return os.path.join(ledger_dir, 'sidecar_heartbeat.json')


def write_heartbeat(ledger_dir, seq, at, writer_token):
    """仅 B_SIDECAR 可写心跳。模拟伴生进程存活信号。"""
    from ledgers import B_SIDECAR
    if writer_token != B_SIDECAR:
        raise PermissionError('E-PERM-DENIED: 只有 B_SIDECAR 可写心跳')
    hb = {'seq': seq, 'at': at, 'writer': writer_token}
    with io.open(heartbeat_path(ledger_dir), 'w', encoding='utf-8') as f:
        f.write(json.dumps(hb, ensure_ascii=False, sort_keys=True) + '\n')
    return hb


def check(ledger_dir, execution_log_path, observed_at, stale_after_seq=2):
    """只读检查。返回结构化报告，含 gap 标记（不修改任何文件）。"""
    hb_path = heartbeat_path(ledger_dir)
    problems = []
    hb = None
    if not os.path.exists(hb_path):
        problems.append('E-HB-MISSING: 心跳文件不存在（Sidecar 从未启动或已被清除）')
    else:
        try:
            hb = json.loads(io.open(hb_path, encoding='utf-8').read().strip())
        except Exception as exc:
            problems.append('E-HB-CORRUPT: %s' % exc)
    entries = chain.read_entries(execution_log_path)
    actual_seq = len(entries)

    gap = {'status': 'OK', 'missing_from': None, 'missing_to': None}
    if hb:
        declared = hb.get('seq', 0)
        if declared < actual_seq:
            problems.append('E-HB-BEHIND: 心跳 seq=%s 落后于账本 %s' % (declared, actual_seq))
        if declared > actual_seq:
            # 心跳说写了更多，但账里没有 → 该段缺失（kill 在写心跳后、落盘前）
            gap = {'status': MISSING, 'missing_from': actual_seq + 1, 'missing_to': declared}
            problems.append('E-SEGMENT-MISSING: 心跳声明到 seq=%s，账本只到 %s，缺 %s 条'
                            % (declared, actual_seq, declared - actual_seq))
        if stale_after_seq and declared and declared == actual_seq:
            # 心跳与账本同步但无新进展——用 observed_at 与 hb.at 比较（调用方显式给）
            if hb.get('at') and observed_at and hb['at'] < observed_at:
                gap['last_heartbeat'] = hb['at']
                gap['observed_at'] = observed_at

    chain_check = chain.verify_chain(execution_log_path)
    if not chain_check['ok']:
        problems.append('E-CHAIN-BROKEN: %s' % '; '.join(chain_check['problems'][:3]))

    return {
        'ok': not problems and gap['status'] == 'OK',
        'problems': problems,
        'gap': gap,
        'declared_seq': hb.get('seq') if hb else None,
        'actual_seq': actual_seq,
        'chain': chain_check,
    }
