#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-WhereB-internal
#
# flash.py — 기기를 굽는 **유일한 진입점**.
#
#   python3 flash.py                          대화형 (사람이 손으로 구울 때)
#   python3 flash.py --role anchor:1          비대화형 (에이전트·스크립트)
#   python3 flash.py --role beacon:2 --sn 123456
#   python3 flash.py --list                   꽂혀 있는 기기와 매핑만 보기
#   python3 flash.py --role bo --dry-run      실제로 굽지 않고 시퀀스만 출력
#
# 예전엔 굽는 도구가 넷이었고 같은 기기를 굽는 방법이 여러 개였다. 어느 것을 집었느냐로
# 결과가 갈렸고("구웠는데 반영이 안 됨"), 그래서 도구마다 경고가 붙었다.
# 지금은 시퀀스가 flashlib.burn() 한 벌이고, 기기별 차이는 flashlib.RECIPES 표에만 있다.
#
# 절차 문서: WhereVehicle 레포 docs/guide/build_flash.md

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import flashlib as F   # noqa: E402


def parse_role(role):
    """'anchor:1' → (recipe, 1). 인스턴스가 필요한데 없으면 None 을 돌려준다."""
    kind, _, arg = (role or "").partition(":")
    r = F.RECIPES.get(kind)
    if not r:
        return None, None, "알 수 없는 role: %s (가능: %s)" % (kind, ", ".join(sorted(F.RECIPES)))
    inst = None
    if r.needs_id:
        if arg == "" or arg.lower() == "auto":
            inst = None          # 비콘: 원장이 빈 번호를 고른다
        elif arg.isdigit():
            inst = int(arg)
        else:
            return None, None, "%s 번호가 필요하다 — 예: --role %s:1 또는 --role %s" % (
                kind, kind, kind)
    return r, inst, ""


def sn_for(recipe, inst):
    """device_serial_mapping.json 이 SN 의 SSOT 다. 문서에 옮겨 적지 않는다."""
    m = F.load_mapping()
    if recipe.key == "anchor":
        return (m.get("anchors") or {}).get("a%d" % inst)
    if recipe.key == "bo":
        return m.get("bo")
    if recipe.key in ("beacon", "tag", "selftest"):
        # K4W·C3W 는 온보드 디버거가 없다 — 외장 하나를 보드 사이에 옮겨 꽂는다.
        return m.get("external_debugger")
    return None


def _report_build_version(recipe, app):
    """이 hex 가 몇 버전인지, 설정과 같은지 **값으로** 말해 준다.

    빌드가 남긴 `<hex>.build.json` 을 읽는다. 예전엔 mtime 만 비교했는데 파일을 만지기만
    해도 어긋나서(테스트가 build_config 를 복원하며 mtime 을 올렸다) 멀쩡한 hex 가
    "낡았다" 로 떴다. 스탬프가 없으면 옛 빌드이므로 그것만 말한다.
    """
    import hashlib
    import json
    cfg_path = os.path.join(F.THIS_DIR, "build_config.yaml")
    cfg, cfg_sha = {}, ""
    try:
        import yaml
        raw = open(cfg_path, "rb").read()
        cfg = yaml.safe_load(raw.decode("utf-8")) or {}
        cfg_sha = hashlib.sha256(raw).hexdigest()[:12]
    except Exception:
        pass
    ver_role = "beacon" if recipe.key in ("beacon", "bo") else recipe.key
    want = ((cfg.get(ver_role) or {}) if isinstance(cfg.get(ver_role), dict)
            else {}).get("fw_version")

    st = {}
    try:
        with open(app + ".build.json", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}

    if not st:
        print("   빌드: 스탬프 없음 — 이 hex 가 어느 설정으로 만들어졌는지 알 수 없다")
        print("      `python3 build.py %s` 로 다시 빌드하면 버전이 표시된다" % recipe.key)
        return

    got = st.get("fw_version")
    line = "   빌드: %s%s · N=%s · sf=%sms · %s" % (
        st.get("role") or recipe.key,
        (" v%s" % got) if got is not None else "",
        st.get("n_anchors"), st.get("sf_period_ms"), st.get("built_at"))
    if want is not None:
        line += "   (설정 v%s)" % want
    print(line)

    if want is not None and got is not None and got != want:
        print("   ⚠ 이 hex 는 v%s 인데 설정은 v%s 다 — 다시 빌드할 것" % (got, want))
        print("      `cd firmware && python3 build.py %s`" % recipe.key)
    elif cfg_sha and st.get("config_sha256") and st["config_sha256"] != cfg_sha:
        print("   ⚠ 빌드 이후 build_config.yaml 이 바뀌었다 (버전은 그대로) — 다시 빌드할 것")
        print("      `cd firmware && python3 build.py %s`" % recipe.key)



def label_from_args(recipe, args):
    """인자로 받은 라벨 → (label[6], serial) 또는 (None, 0).

    ⚠ **왜 인자로 받아야 하는가**: 예전에는 빈 칩의 라벨을 대화형에서만 받았다. 그래서
    자동화(스크립트·에이전트)는 라벨을 넣을 방법이 없어 대화형 메뉴에 답을 파이프로 밀어
    넣었고, 프롬프트가 하나 어긋나 **도메인이 엉뚱한 값으로 새겨졌다**(실제 사고).
    사람용 대화형은 편의이고, 기계용 경로는 인자다 — 둘 다 있어야 한다.
    """
    qr = getattr(args, "qr", None)
    psn = getattr(args, "printed_sn", None)
    if recipe.label_kind == F.BOARD_TYPE_CDK:
        if psn:
            lab = F.parse_printed_sn(psn)
            if not lab:
                print("   ✗ --printed-sn 이 10자리 hex 가 아니다: %s" % psn)
                return None, 0
            return lab, 0
        if qr:
            print("   ✗ 이 보드의 라벨은 인쇄 SN 이다 — --printed-sn 을 쓸 것")
        return None, 0
    if qr:
        got = F.parse_mac_serial(qr)
        if not got:
            print("   ✗ --qr 을 못 읽었다 (형식: 'MAC:BC572926709E,SERIAL:773686')")
            return None, 0
        return got
    if psn:
        print("   ✗ 이 보드의 라벨은 케이스 MAC+SERIAL 이다 — --qr 을 쓸 것")
    return None, 0


def do_flash(recipe, inst, sn, args, interactive_ok=False):
    rec = None
    why = ""
    domain = getattr(args, "domain", None) or ""
    new_label, new_serial = None, 0
    if not args.dry_run and recipe.label_kind != F.BOARD_TYPE_UNKNOWN:
        rec, why = F.probe_label(sn)
        # [빈 칩] 라벨이 없으면 **여기서** 받는다. 비콘 번호는 SERIAL 로 부여되므로
        # 라벨이 번호 배정보다 먼저 있어야 한다. recover(mass erase) 도중에 중단하면
        # UICR 이 지워져 멀쩡한 보드가 이 상태로 보인다 — 그때 다시 새겨 넣는 경로다.
        if rec is None:
            new_label, new_serial = label_from_args(recipe, args)
            if new_label:
                print("   라벨이 없는 보드다(%s) — 인자로 받은 값을 새긴다." % why)
            elif interactive_ok:
                print("   라벨이 없는 보드다(%s) — 케이스 정보를 먼저 받는다." % why)
                new_label, new_serial = F.prompt_label(recipe.label_kind, log=print)

    # 비콘 번호는 원장이 SERIAL 기준으로 부여한다. 컴파일 타임 BEACON_ID 가 아니다.
    if recipe.key == "beacon" and not args.dry_run:
        domain = (domain
                  or (rec or {}).get("domain")
                  or ((F.known_domains() or [""])[0]))
        serial = (rec or {}).get("serial") or new_serial
        if serial:
            assigned, err = F.assign_beacon_id(
                F.load_ledger(), serial, domain,
                requested=inst, force=getattr(args, "force", False))
            if err:
                print("   ✗ %s" % err)
                return False
            if inst is not None and assigned != inst:
                print("   요청 id=%s → 부여 id=%s" % (inst, assigned))
            elif inst is None:
                print("   자동 부여: %s %d  (SERIAL %s)" % (domain, assigned, serial))
            inst = assigned
        elif inst is None:
            print("   ✗ SERIAL 을 못 읽어 번호를 부여할 수 없다 — %s" % why)
            return False

    # 비콘 앱은 UICR 의 beacon_id 를 쓰므로 hex 는 id 와 무관. 1번 이미지를 공통으로 쓴다.
    hex_inst = 1 if recipe.key == "beacon" else inst
    app = args.hex or F.resolve_hex(recipe, hex_inst, args.board)
    print("── %s%s · board=%s · SN=%s" % (
        recipe.key, (":%d" % inst) if inst else "", args.board or recipe.board, sn))
    print("   hex: %s" % os.path.relpath(app, F.THIS_DIR))
    # [#29] **빌드가 설정보다 오래됐으면 말해 준다.** 옛 hex 를 그대로 구워도 도구는
    # "✓ 완료" 라고 하므로, 기능이 빠진 줄 모르고 케이스를 닫게 된다(실제로 b2·b4 의
    # hex 가 5시간 전 빌드였다).
    _report_build_version(recipe, app)

    # 라벨은 굽고 나서 새긴다(recover 가 UICR 을 지우므로). 빈 칩이면 위에서 이미 받았다.
    if not args.dry_run and recipe.label_kind != F.BOARD_TYPE_UNKNOWN:
        # [#29] **무엇을 손에 들고 있는지 먼저 말해 준다.**
        if rec and not check_identity(recipe, inst, rec, interactive_ok,
                                      getattr(args, "force", False)):
            return False
        if rec and not args.relabel:
            pass                       # MAC·SERIAL 보존. beacon_id 는 burn 이 inst 로 덮을 수 있다.
        elif rec and args.relabel and not new_label:
            # 인자로 준 값이 이긴다. 없으면 대화형에서만 묻는다(비대화형은 아래에서 경고).
            new_label, new_serial = label_from_args(recipe, args)
            if new_label:
                print("   ⚠ --relabel — 기존 라벨을 덮어씁니다: %s" % F.label_str(rec))
            elif interactive_ok:
                print("   ⚠ --relabel — 기존 라벨을 덮어씁니다: %s" % F.label_str(rec))
                new_label, new_serial = F.prompt_label(recipe.label_kind, log=print)
        elif interactive_ok:
            pass
            if new_label:
                doms = F.known_domains()
                # 이미 고른 도메인이 있으면 그것이 이긴다 — 여기서 기본값으로
                # 되돌리면 앞에서 고른 값(예: allcargo)이 조용히 사라진다.
                domain = domain or (rec or {}).get("domain") or (doms[0] if doms else "")
                if doms and not getattr(args, "domain", None):
                    v = ask("   도메인 [%s]: " % domain).strip()
                    domain = v or domain
        elif not rec:
            print("   ⚠ 라벨이 없는 보드입니다(%s) — 비대화형이라 새기지 않고 굽습니다." % why)
            print("      라벨을 새기려면 인자 없이 `python3 flash.py` 로 대화형 실행")

    ok, err = F.burn(sn, app,
                     label=new_label, serial=new_serial, domain=domain,
                     bootloader=not args.no_bootloader,
                     settings_hex=getattr(args, "settings", None),
                     keep_label=not args.relabel,
                     board_type=recipe.label_kind,
                     role=recipe.key,          # 우리 부트로더는 비콘만 받는다 (flashlib 주석)
                     beacon_id=inst or 0,
                     dry_run=args.dry_run,
                     log=print)
    print(("   ✓ 완료" if ok else "   ✗ 실패 — %s" % err))

    # [#29] 구운 뒤 **기기에서 다시 읽어** 기록한다 — 기록의 근거는 언제나 기기다.
    # 매번 보드를 물어 SERIAL 을 확인하는 수고를 덜기 위한 것이고, SSOT 는 여전히 UICR 이다.
    if ok and not args.dry_run:
        rec, _why = F.probe_label(sn)
        bl_ours = os.path.abspath(F.find_bootloader_hex(recipe.key)).startswith(
            os.path.abspath(os.path.join(F.REPO_DIR, "firmware", "bootloader")))
        F.record_flash(recipe.key, inst, args.board or recipe.board, rec, app,
                       bl_ours and not args.no_bootloader, log=print)
        if rec:
            print("   기록: %s" % F.label_str(rec))
    return ok


def check_identity(recipe, inst, rec, interactive_ok, force=False):
    """읽은 UICR 을 보여주고, 지난 기록과 다른 번호면 확인받는다. False = 중단."""
    serial = rec.get("serial")
    print("   보드: %s (도메인 %s)" % (F.label_str(rec), rec.get("domain") or "미기록"))
    prev_key, prev = F.ledger_find_by_serial(serial)
    cur_key = "%s:%d" % (recipe.key, inst) if inst else recipe.key
    if not prev_key:
        return True
    print("   지난 기록: %s 로 구웠음 (%s)" % (prev_key, prev.get("at")))
    if prev_key == cur_key:
        return True
    print("   ⚠ **지금은 %s 로 구우려 한다** — 같은 보드에 다른 번호를 새기는 것이다." % cur_key)
    print("      번호가 겹치면 두 대가 같은 id 로 떠서 화면·측위가 섞인다.")
    if force:
        print("      --force 로 진행한다.")
        return True
    if not interactive_ok:
        print("      비대화형이라 중단한다. 의도한 것이면 --force 로 다시 실행.")
        return False
    return ask("      계속할까요? [y/N]: ").strip().lower() in ("y", "yes")


def cmd_list():
    m = F.load_mapping()
    live = F.connected_jlinks()
    print("연결된 J-Link: %s" % (", ".join(live) if live else "(없음)"))
    print("\n매핑 (device_serial_mapping.json):")
    for k, v in (m.get("anchors") or {}).items():
        if not k.startswith("_"):
            print("  anchor %-4s %s%s" % (k, v, "   ← 연결됨" if str(v) in live else ""))
    if m.get("bo"):
        print("  bo         %s%s" % (m["bo"], "   ← 연결됨" if str(m["bo"]) in live else ""))
    if m.get("external_debugger"):
        d = m["external_debugger"]
        print("  외장디버거  %s%s   (K4W 비콘·C3W 태그용)"
              % (d, "   ← 연결됨" if str(d) in live else ""))
    # [#29] 외장 디버거에 보드가 물려 있으면 **지금 손에 든 것**을 읽어 준다.
    # "무엇을 굽고 있는지" 를 굽기 전에 아는 것이 이 도구의 첫 번째 일이다.
    ext = m.get("external_debugger")
    if ext and str(ext) in live:
        rec, why = F.probe_label(ext)
        if rec:
            prev_key, prev = F.ledger_find_by_serial(rec.get("serial"))
            print("\n지금 물려 있는 보드: %s (도메인 %s)%s"
                  % (F.label_str(rec), rec.get("domain") or "미기록",
                     ("  ← 지난번 %s" % prev_key) if prev_key else ""))
        else:
            print("\n지금 물려 있는 보드: UICR 못 읽음 — %s" % why)

    led = F.load_ledger()
    rows = sorted((led.get("by_serial") or {}).values(),
                  key=lambda v: ((v.get("domain") or ""), v.get("role") or "", v.get("id") or 0))
    if rows:
        print("\n원장 파일: %s" % os.path.relpath(F.LEDGER_PATH, F.THIS_DIR))
        print("원장 (SERIAL 키 — SSOT 아님, 원본은 케이스/UICR):")
        for v in rows:
            print("  %-8s %-2s  SERIAL %-8s %s  %s  (%s)" % (
                v.get("domain") or "-",
                v.get("id") if v.get("id") is not None else "-",
                v.get("serial"), v.get("mac") or "",
                v.get("role") or "", v.get("at") or ""))

    print("\n레시피:")
    for k, r in sorted(F.RECIPES.items()):
        print("  %-9s board=%-4s %s" % (k, r.board, r.desc))


class _Abort(Exception):
    """사람이 Ctrl-C·Ctrl-D 로 그만둔 것 — 트레이스백을 보여줄 일이 아니다."""


def ask(prompt=""):
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise _Abort


def _pick_hex(recipe, inst, board, ask):
    """변형이 여럿이면 어느 hex 를 구울지 번호로 고르게 한다.

    태그는 tag.hex(시험) · tag_shadow.hex(음영전용) · tag_field.hex(현장) 처럼 변형이
    한 디렉토리에 같이 있다. 대화형에서 못 고르면 "라벨을 새기려면 대화형, 변형을 고르려면
    비대화형" 이라는 양립 불가가 생긴다 — 그래서 여기서 묻는다.
    반환 = 고른 hex 경로(정규본이면 None → 호출측 기본 해석에 맡긴다).
    """
    try:
        canon = F.resolve_hex(recipe, 1 if recipe.key == "beacon" else inst, board)
    except Exception:
        return None
    d = os.path.dirname(canon)
    stem = os.path.basename(canon)[:-4]
    cands = sorted(g for g in glob.glob(os.path.join(d, stem + "*.hex"))
                   if not g.endswith(".settings.hex"))
    if len(cands) <= 1:
        return None
    print("\n   어느 빌드로?")
    for n_, c in enumerate(cands, 1):
        tail = "  ← 기본" if os.path.abspath(c) == os.path.abspath(canon) else ""
        print("  %d) %s%s" % (n_, os.path.basename(c), tail))
    v = ask("번호 [Enter=%s]: " % os.path.basename(canon)).strip()
    if v.isdigit() and 1 <= int(v) <= len(cands):
        return cands[int(v) - 1]
    return None


def _ask_domain(rec, is_namespace, ask):
    """UICR 에 새길 도메인을 번호로 고르게 한다. 이름 입력은 새 도메인일 때만.

    보드에 이미 있는 값을 말없이 쓰면, 다른 현장에 갈 보드가 남의 도메인에 들어간다
    (실제로 portSAGE 5 가 그렇게 배정됐다). 비콘에서는 이 값이 beacon_id 의 네임스페이스라
    번호보다 먼저 물어야 한다. 태그는 번호가 없지만 원장·분류 기준이 되므로 같이 묻는다.
    """
    cur = (rec or {}).get("domain") or ""
    doms = F.known_domains() or []
    if cur and cur not in doms:
        doms = [cur] + doms
    dflt = cur or (doms[0] if doms else "")
    print("\n   도메인?%s" % ("  (= beacon_id 네임스페이스)" if is_namespace else ""))
    for n_, d_ in enumerate(doms, 1):
        print("  %d) %-10s%s" % (n_, d_, "  ← 이 보드" if d_ == cur else ""))
    print("  %d) 새 도메인 입력" % (len(doms) + 1))
    v = ask("번호%s: " % ((" [Enter=%s]" % dflt) if dflt else "")).strip()
    if v == "":
        sel = dflt
    elif v.isdigit() and 1 <= int(v) <= len(doms):
        sel = doms[int(v) - 1]
    elif v.isdigit() and int(v) == len(doms) + 1:
        sel = ask("   새 도메인 이름: ").strip() or dflt
    else:
        sel = v                     # 이름을 그대로 친 경우도 받는다
    if sel and sel not in doms:
        print("   ⚠ 새 도메인: %s  (대소문자가 다르면 다른 도메인이다)" % sel)
    if sel != cur:
        print("   도메인 변경: %s → %s" % (cur or "미기록", sel))
    return sel


def interactive(default_domain=""):
    """[#29] **장치 타입이 첫 질문이다.**

    사람이 하는 일의 순서는 "보드를 집어 든다 → 이게 뭐였더라 → 그거로 굽는다" 다.
    도구가 역할부터 물으면 기억에 의존해 번호를 고르게 되고, 그러다 **b4 보드에 beacon:3 을
    구웠다**(260819). 그래서:

      1) 보드 타입(k4w / c3w / cdk)을 고른다
      2) k4w·c3w → 디버거는 외장 1개뿐이라 고를 것이 없다. 바로 UICR 을 읽어 SERIAL·MAC 과
         **지난 기록**을 보여주고, 그 기록을 역할·번호 기본값으로 제시한다(Enter=그대로)
      3) cdk → 그때 J-Link 목록을 읽어 SN 으로 고르게 한다(온보드 디버거가 여럿이다)

    ⚠ 첫 화면에서 J-Link 를 열거하지 않는다 — 필요한 시점에만 읽는다.
    """
    print("=== 기기 굽기 ===")

    boards = []
    for b in ("k4w", "c3w", "cdk"):
        roles = sorted(k for k, r in F.RECIPES.items() if r.board == b)
        if roles:
            boards.append((b, roles))
    for n, (b, roles) in enumerate(boards, 1):
        print("  %d) %-4s  %s" % (n, b, " / ".join(roles)))
    sel = ask("\n보드 타입? [번호 또는 이름]: ").strip()
    board = None
    if sel.isdigit() and 1 <= int(sel) <= len(boards):
        board = boards[int(sel) - 1][0]
    elif sel in dict(boards):
        board = sel
    if not board:
        print("알 수 없는 보드: %s" % sel)
        return 1
    roles = dict(boards)[board]

    m = F.load_mapping()
    rec, sn, prev_key = None, None, None

    if board in ("k4w", "c3w"):
        # 외장 디버거 하나뿐 — 고를 것이 없다.
        sn = m.get("external_debugger")
        if not sn:
            print("매핑에 external_debugger 가 없다 — device_serial_mapping.json 확인")
            return 1
        # 이미 물려 있는 것이 정상이다 — **묻지 않고 바로 읽는다.**
        # 못 읽었을 때만 연결을 요청한다(그때는 물어볼 이유가 있다).
        rec, why = F.probe_label(sn)
        if not rec:
            ask("\n   보드가 안 읽힌다(%s) — 외장 디버거(SN %s)에 연결한 뒤 Enter: "
                % (why, sn))
            rec, why = F.probe_label(sn)
        if rec:
            print("   손에 든 보드: %s (도메인 %s)"
                  % (F.label_str(rec), rec.get("domain") or "미기록"))
            prev_key, prev = F.ledger_find_by_serial(rec.get("serial"))
            if prev_key:
                print("   지난 기록: **%s** (%s, %s)"
                      % (prev_key, prev.get("hex"), prev.get("at")))
            else:
                print("   지난 기록: 없음 — 이 보드를 구운 기록이 이 도구에 없다")
        else:
            print("   손에 든 보드: UICR 못 읽음 — %s" % why)
    else:
        # cdk: 온보드 디버거가 여럿이라 SN 으로 고른다.
        live = F.connected_jlinks()
        names = F.jlink_table()
        if not live:
            print("\n연결된 J-Link 가 없다 — 케이블 확인 후 다시.")
            return 1
        print("\n연결된 J-Link:")
        for n, s_ in enumerate(live, 1):
            print("  %d) %-10s %s" % (n, s_, names.get(s_, "(미매핑)")))
        v = ask("어느 것? [번호 또는 SN]: ").strip()
        if v.isdigit() and 1 <= int(v) <= len(live):
            sn = live[int(v) - 1]
        elif v in live:
            sn = v
        if not sn:
            print("알 수 없는 선택: %s" % v)
            return 1
        # 매핑 이름에서 역할·번호를 추론해 기본값으로 쓴다.
        nm = names.get(sn, "")
        if nm.startswith("anchor a"):
            prev_key = "anchor:%s" % nm.split("a")[-1]
        elif nm == "bo":
            prev_key = "bo"

    # 역할·번호 — 기록/매핑이 기본값이다.
    print("\n무엇으로 구울까요?")
    for n_, rn_ in enumerate(roles, 1):
        mark = "  ← 지난번" if (prev_key and prev_key.split(":", 1)[0] == rn_) else ""
        print("  %d) %s%s" % (n_, rn_, mark))
    ans = ask("번호 또는 이름%s: "
              % ((" [Enter=%s]" % prev_key) if prev_key else "")).strip()
    if ans.isdigit() and 1 <= int(ans) <= len(roles):
        ans = roles[int(ans) - 1]
    explicit = bool(ans)
    if not ans and prev_key:
        ans = prev_key
    if not ans:
        print("선택이 필요합니다")
        return 1

    kind, _, arg = ans.partition(":")
    r = F.RECIPES.get(kind)
    if not r:
        print("알 수 없는 역할: %s (이 보드: %s)" % (kind, " ".join(roles)))
        return 1
    if r.board != board and kind not in roles:
        print("⚠ %s 는 %s 보드용이 아니다 (이 보드: %s)" % (kind, board, " ".join(roles)))
        return 1

    inst = None
    sel_domain = default_domain or ""
    # 도메인은 UICR 에 새겨지는 **보드 정체성**이다 — 비콘만의 것이 아니다(태그도 새긴다).
    # 비콘에서는 beacon_id 의 네임스페이스까지 정하므로 번호를 묻기 전에 와야 한다.
    if r.label_kind != F.BOARD_TYPE_UNKNOWN and not sel_domain:
        sel_domain = _ask_domain(rec, r.needs_id, ask)
    if r.needs_id:
        suggest = arg if arg.isdigit() else (
            prev_key.split(":", 1)[1] if (prev_key and prev_key.startswith(kind + ":")) else "")
        if kind == "beacon":
            if arg.isdigit():
                inst = int(arg)
            else:
                v = ask("%s 번호 [Enter=자동%s]: " % (
                    kind, (" / 지난번 %s" % suggest) if suggest else "")).strip()
                if v == "" and suggest and prev_key:
                    inst = int(suggest)
                elif v == "" or v.lower() == "auto":
                    inst = None
                elif v.isdigit():
                    inst = int(v)
                else:
                    print("번호가 필요합니다")
                    return 1
        else:
            v = arg if arg.isdigit() else (ask(
                "%s 번호%s: " % (kind, (" [%s]" % suggest) if suggest else "")).strip() or suggest)
            if not str(v).isdigit():
                print("번호가 필요합니다")
                return 1
            inst = int(v)

    if sn is None:
        sn = sn_for(r, inst) or ask("J-Link SN: ").strip()

    sel_hex = _pick_hex(r, inst, board, ask)

    class A:
        hex = sel_hex; no_bootloader = False; relabel = False
        dry_run = False; settings = None
        # 지난 기록을 **보고 나서** 다른 것을 골랐다면 그게 곧 확인이다.
        force = explicit
    A.board = board
    A.domain = sel_domain
    return 0 if do_flash(r, inst, sn, A(), interactive_ok=True) else 1


HELP_EPILOG = """
두 가지 쓰는 법
---------------
  python3 flash.py                     인자 없음 = **사람용 대화형**. 보드·역할·도메인·라벨을
                                       차례로 묻는다. 손에 든 보드가 무엇인지 먼저 읽어 준다.
  python3 flash.py --role ... [인자]    인자를 주면 **끝까지 비대화형**으로 진행한다.
                                       스크립트·자동화는 이쪽만 쓸 것 — 대화형 메뉴에 답을
                                       파이프로 밀어 넣으면 프롬프트가 하나 어긋나는 순간
                                       엉뚱한 값이 UICR 에 새겨진다(실제로 도메인이 그렇게 깨졌다).

자주 쓰는 예
------------
  # 앵커 (번호는 SN 매핑에서 찾는다)
  python3 flash.py --role anchor:1
  python3 flash.py --role anchor:1 --hex /tmp/a1.hex        # 다른 곳에서 빌드한 hex

  # BO (수집기)
  python3 flash.py --role bo

  # 비콘 — 라벨이 이미 있는 보드
  python3 flash.py --role beacon:5 --domain portSAGE

  # 비콘 — **빈 칩**(라벨 없음): 케이스 QR 을 인자로 준다
  python3 flash.py --role beacon --domain portSAGE \
                   --qr 'MAC:BC572926709E,SERIAL:773686'
  #   └ 번호를 생략하면 원장이 그 도메인의 빈 번호를 부여한다

  # 태그 (C3W) — 라벨은 케이스 QR
  python3 flash.py --role tag --qr 'MAC:BC57291D0B58,SERIAL:647130'

  # CDK 보드에 라벨을 새길 때는 보드에 **인쇄된 10자리 hex SN**
  python3 flash.py --role anchor:2 --printed-sn A1B2C3D4E5

  # 굽지 않고 확인만
  python3 flash.py --list                                   # 연결 기기·매핑·레시피
  python3 flash.py --role beacon:5 --dry-run                # 시퀀스만

알아 둘 것
----------
  * 라벨(UICR)은 기본 **보존**된다. 덮어쓰려면 --relabel 과 함께 --qr/--printed-sn 을 준다.
  * 도메인은 대소문자가 다르면 **다른 도메인**이다(기기에 새겨지는 값). portSAGE ≠ portsage.
  * 비콘 번호는 컴파일 타임이 아니라 **UICR** 값이다 — hex 는 1번 이미지를 공통으로 쓴다.
  * 비콘·태그는 외장 J-Link 하나를 옮겨꽂으므로 **한 번에 한 대**다.
"""


def main():
    ap = argparse.ArgumentParser(
        description="기기를 굽는 유일한 진입점. 인자 없이 실행하면 사람용 대화형, "
                    "인자를 주면 비대화형으로 끝까지 진행한다.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", metavar="ROLE",
                    help="무엇으로 구울까: anchor:N | beacon[:N] | tag | bo | selftest. "
                         "비콘 번호를 생략하면 원장이 빈 번호를 부여한다")
    ap.add_argument("--sn", help="J-Link SN. 생략하면 device_serial_mapping.json 에서 찾는다 "
                                 "(비콘·태그는 external_debugger)")
    ap.add_argument("--hex", help="app hex 직접 지정. 다른 기기에서 빌드한 hex 를 굽을 때")
    ap.add_argument("--board", metavar="cdk|k4w|c3w",
                    help="보드 override. 보통 역할이 정하므로 줄 필요가 없다")
    ap.add_argument("--qr", metavar="TEXT",
                    help="케이스 QR 문자열 = 라벨(MAC+SERIAL). 형식 "
                         "'MAC:BC572926709E,SERIAL:773686'. **빈 칩을 비대화형으로 구울 때 필수** "
                         "(K4W 비콘·C3W 태그)")
    ap.add_argument("--printed-sn", metavar="HEX10",
                    help="CDK 보드에 인쇄된 10자리 hex SN = 그 보드의 라벨 (J-Link SN 이 아니다)")
    ap.add_argument("--domain", metavar="NAME",
                    help="UICR 도메인 = beacon_id 네임스페이스. 기본은 보드에 있는 값, "
                         "없으면 원장의 첫 항목. ⚠ 대소문자가 다르면 다른 도메인이다")
    ap.add_argument("--relabel", action="store_true",
                    help="UICR 라벨을 새로 기록(기본은 기존 값 보존). --qr/--printed-sn 과 함께 쓴다")
    ap.add_argument("--no-bootloader", action="store_true",
                    help="부트로더 없이 SD+app 만. ⚠ 이후 원격 OTA 불능")
    ap.add_argument("--settings", metavar="HEX",
                    help="미리 만든 bootloader settings hex (nrfutil 이 없는 기기에서 굽을 때)")
    ap.add_argument("--make-settings", metavar="OUT",
                    help="굽지 않고 settings hex 만 만든다 (nrfutil 이 있는 곳에서)")
    ap.add_argument("--force", action="store_true",
                    help="지난 기록과 다른 번호·보드로 굽는 것을 허용(비대화형 확인 생략)")
    ap.add_argument("--dry-run", action="store_true",
                    help="굽지 않고 시퀀스만 출력 (기기를 안 건드린다)")
    ap.add_argument("--list", action="store_true",
                    help="연결 기기·매핑·원장·레시피 보기")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return 0
    if args.make_settings:
        r, inst, err = parse_role(args.role or "")
        if err:
            print(err); return 2
        app = args.hex or F.resolve_hex(r, inst, args.board)
        ok, e = F.generate_settings(app, args.make_settings)
        print(("✓ %s" % args.make_settings) if ok else ("✗ %s" % e))
        return 0 if ok else 1
    if not args.role:
        try:
            return interactive(args.domain or "")
        except _Abort:
            print("\n중단했습니다.")
            return 130

    r, inst, err = parse_role(args.role)
    if err:
        print(err)
        return 2
    sn = args.sn or sn_for(r, inst)
    if not sn:
        print("SN 을 못 찾았다 — --sn 으로 지정하거나 device_serial_mapping.json 확인")
        return 2
    return 0 if do_flash(r, inst, sn, args) else 1


if __name__ == "__main__":
    sys.exit(main())
