# -*- coding: utf-8 -*-
"""B1 写入隔离判据检验器（2026-11-25）

B1 硬判据：模拟 Agent 直接写日志被【系统权限】拒绝。
不是"换了个进程写"（那是 B0 的 writer_token 模拟）。

本脚本提供两部分：
  ① check_dir()：对给定目录做只读拒写检验（真实 open 尝试，不靠 os.access 猜）
  ② self_test()：在本地 sandbox 证明检验器能正确区分可写/只读

**诚实边界（不许省）**：
  - 本地 self_test 只证明"检验器可用"，不证明任何节点达成 B1；
  - Windows 只读属性 ≠ 账号级隔离（owner 可随时改回）——本项目已固化
    "owner 位优先于 ACL"铁律，故真实 B1 需【独立账号 + 宿主对该目录无写权限】
    的环境证据，本脚本只做可执行的检测手段，不替代那个证据；
  - 只读挂载（U 盘场景）是比 ACL 更硬的物理边界，但同样要实测，不宣称。

用法：
  python3 -X utf8 verify_b1.py                 # 本地自测（检验器可用性）
  python3 -X utf8 verify_b1.py --check <dir>   # 对某目录做只读拒写检验
"""
import io
import json
import os
import stat
import sys
import tempfile


def check_dir(dirpath, probe_name='.b1_write_probe'):
    """实测：尝试在该目录创建/写文件，判断写入是否被拒。

    返回结构化结果。只读属性由外部（挂载/ACL/账号权限）提供，本函数不改权限。
    """
    path = os.path.join(dirpath, probe_name)
    result = {'dir': dirpath, 'writable': None, 'evidence': '', 'note': ''}
    try:
        with io.open(path, 'w', encoding='utf-8') as f:
            f.write('probe')
        os.remove(path)
        result['writable'] = True
        result['evidence'] = 'E-B1-WRITABLE: 执行者可写该目录 → B1 不成立'
        result['note'] = '写探针成功创建并删除'
    except (PermissionError, OSError) as e:
        result['writable'] = False
        result['evidence'] = 'E-B1-READONLY: 写入被系统拒绝 → 满足 B1 判据之一'
        result['note'] = '%s: %s' % (type(e).__name__, str(e)[:80])
    result['ok_for_b1'] = (result['writable'] is False)
    # 关键提醒：单靠这个结果不能宣告 B1 达成
    result['caveat'] = ('只读拒写是 B1 的必要判据，非充分条件；'
                        '还需证明写入由独立系统账号/只读挂载完成，且 owner 无法绕过。')
    return result


def self_test():
    results = []
    def ck(name, cond, detail=''):
        results.append((name, bool(cond)))
        print(('PASS ' if cond else 'FAIL ') + name + ((' | ' + detail) if detail else ''))

    tmp = tempfile.mkdtemp(prefix='b1_probe_')
    try:
        # ① 可写目录 → 检验器应报 writable=True、ok_for_b1=False
        r_w = check_dir(tmp, '.probe_writable')
        ck('检验器：可写目录正确识别为不安全', r_w['writable'] is True
           and r_w['ok_for_b1'] is False, r_w['evidence'])

        # ② 真实拒写场景 → 检验器应报写入被拒、ok_for_b1=True
        #    注意：Windows 下 os.chmod 只读属性挡不住 owner 写入（本项目已固化
        #    "owner 位优先于 ACL"铁律），故不能用 chmod 造假的"只读"，那会误导。
        #    改用真实 OS 级拒写：路径指向一个【文件】而非目录 —— open 子路径必失败。
        blocker = os.path.join(tmp, 'this_is_a_file')
        with io.open(blocker, 'w', encoding='utf-8') as f:
            f.write('blocker')
        r_r = check_dir(blocker, '.probe_readonly')   # dirpath 其实是文件 → 写入必被 OS 拒
        ck('检验器：真实拒写场景识别为安全', r_r['writable'] is False
           and r_r['ok_for_b1'] is True, r_r['evidence'])
        ck('检验器：拒写结果带 caveat（不冒充 B1 达成）',
           bool(r_r['caveat']) and '必要判据' in r_r['caveat'])

        # ③ 诚实性探针：Windows chmod 只读挡不住 owner —— 明确暴露这个事实，
        #    防未来有人用 chmod 造"假只读"来宣称 B1 达成。
        ro = os.path.join(tmp, 'chmod_readonly')
        os.makedirs(ro, exist_ok=True)
        os.chmod(ro, stat.S_IREAD | stat.S_IEXEC)
        try:
            r_chmod = check_dir(ro, '.probe_chmod')
            owner_bypass = (r_chmod['writable'] is True)  # 若挡不住 → 证据
            ck('诚实性：chmod 只读属性不可冒充账号级隔离（owner 可绕过）',
               owner_bypass or os.name != 'nt',
               'chmod 后仍可写=%s（Windows owner 特性）' % owner_bypass)
        finally:
            os.chmod(ro, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    p = sum(1 for _, ok in results if ok)
    print('\nverify_b1 self-test: %d/%d PASS' % (p, len(results)))
    print('提醒：本地自测只证明检验器可用，不证明任何节点达成 B1。')
    return 0 if p == len(results) else 1


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--check':
        r = check_dir(sys.argv[2])
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0)
    sys.exit(self_test())
