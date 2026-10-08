# SPDX-License-Identifier: LicenseRef-WhereB-internal
#
# flashlib — 굽기 로직 한 벌. 진입점은 flash.py 하나다.
#
# 왜 한 파일인가
# --------------
# 예전엔 굽는 도구가 넷이었다(flash_tool·flash_all_anchors·flash_beacon·flash_tag).
# 넷 다 자기 JLink 호출을 갖고 있었고, **같은 앵커를 굽는데도 한쪽은 `erase`, 다른 쪽은
# `recover` 를 썼다.** recover 를 안 하면 lock 된 칩에서 write 가 조용히 실패하는데
# rc=0 이라 "구웠는데 반영이 안 됨" 으로 나타난다. 어느 도구를 집었느냐로 결과가 갈렸다.
#
# 그래서 "무엇을 굽나"(레시피)와 "어떻게 굽나"(시퀀스)를 분리하고, 시퀀스는 여기 한 벌만 둔다.
# 기기가 늘어도 레시피 한 줄이 늘 뿐이다.

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_DIR = os.path.dirname(THIS_DIR)
SDK_DIR = os.environ.get("QM33_SDK_DIR") or os.path.join(REPO_DIR, "ref", "DW3_QM33_SDK_1.1.1")
DEVICE_MAPPING_FN = os.path.join(THIS_DIR, "device_serial_mapping.json")

JLINK_EXE = os.environ.get("JLINK_EXE", "JLinkExe")
DEVICE = "nRF52833_xxAA"
SPEED = "4000"

# ── UICR 라벨 레코드 (SSOT = shared/boards/wb_board_id.h) ─────────────────────
UICR_ID_BASE = 0x10001080
ID_MAGIC = 0x57424C42
ID_VERSION = 4
ID_WORDS_V3 = 9
ID_WORDS = 10
ID_DOMAIN_LEN = 16

BOARD_TYPE_UNKNOWN = 0
BOARD_TYPE_CDK = 1     # label = 00 + 보드 인쇄 10자리 hex SN, serial=0
BOARD_TYPE_K4W = 2     # label = 케이스 MAC
BOARD_TYPE_C3W = 3     # label = 케이스 MAC
BOARD_TYPE_NAME = {BOARD_TYPE_UNKNOWN: "unknown", BOARD_TYPE_CDK: "cdk",
                   BOARD_TYPE_K4W: "k4w", BOARD_TYPE_C3W: "c3w"}
BOARD_TYPE_BY_NAME = {v: k for k, v in BOARD_TYPE_NAME.items()}


def _id_crc(words, n=None):
    n = ID_WORDS if n is None else n
    c = 0x9E3779B9
    for i in range(n - 1):
        c ^= words[i]
        c = ((c << 7) | (c >> 25)) & 0xFFFFFFFF
        c = (c + 0x85EBCA6B) & 0xFFFFFFFF
    return c


def id_pack(label, serial, beacon_id=0, domain="", board_type=BOARD_TYPE_UNKNOWN):
    """label 6바이트의 뜻은 board_type 이 정한다 — wb_board_id.h 주석 참조."""
    w = [0] * ID_WORDS
    w[0] = ID_MAGIC
    w[1] = label[2] | (label[3] << 8) | (label[4] << 16) | (label[5] << 24)
    w[2] = (label[0] | (label[1] << 8)
            | ((ID_VERSION & 0xFF) << 16) | ((int(beacon_id) & 0xFF) << 24))
    w[3] = int(serial) & 0xFFFFFFFF
    d = (domain or "").encode("ascii", "ignore")[:ID_DOMAIN_LEN]
    for b, ch in enumerate(d):
        w[4 + (b >> 2)] |= ch << ((b & 3) * 8)
    w[8] = int(board_type) & 0xFF
    w[9] = _id_crc(w, ID_WORDS)
    return w


def id_unpack(words):
    """v3(9워드)·v4(10워드) 둘 다 읽는다 — 이미 새겨진 보드를 다시 굽지 않아도 되게.
    ⚠ 버전을 먼저 봐야 워드 수가 정해진다(v3 은 [8] 이 crc)."""
    if not words or len(words) < ID_WORDS_V3 or words[0] != ID_MAGIC:
        return None
    ver = (words[2] >> 16) & 0xFF
    n = ID_WORDS if ver >= 4 else ID_WORDS_V3
    if len(words) < n or words[n - 1] != _id_crc(words, n):
        return None
    label = [words[2] & 0xFF, (words[2] >> 8) & 0xFF,
             words[1] & 0xFF, (words[1] >> 8) & 0xFF,
             (words[1] >> 16) & 0xFF, (words[1] >> 24) & 0xFF]
    raw = bytearray()
    for b in range(ID_DOMAIN_LEN):
        ch = (words[4 + (b >> 2)] >> ((b & 3) * 8)) & 0xFF
        if ch in (0x00, 0xFF):
            break
        raw.append(ch)
    return {"label": label, "mac": label,   # "mac" 은 label 이 MAC 인 보드용 별칭
            "serial": words[3],
            "board_type": (words[8] & 0xFF) if ver >= 4 else BOARD_TYPE_UNKNOWN,
            "beacon_id": (words[2] >> 24) & 0xFF,
            "version": ver,
            "domain": raw.decode("ascii", "ignore")}


def mac_str(mac):
    """케이스 표기 순서 그대로. label 이 MAC 인 보드에서만 뜻이 있다."""
    return ":".join("%02X" % b for b in mac)


def label_str(rec):
    """레코드를 사람이 읽는 한 줄로. 해석은 board_type 이 정한다."""
    if not rec:
        return "(없음)"
    bt = rec.get("board_type", BOARD_TYPE_UNKNOWN)
    lab = rec["label"]
    if bt == BOARD_TYPE_CDK:
        return "SN %s" % "".join("%02X" % b for b in lab[1:])
    mac = ":".join("%02X" % b for b in lab)
    return "MAC %s%s" % (mac, (" SERIAL %d" % rec["serial"]) if rec["serial"] else "")


# ── JLink ────────────────────────────────────────────────────────────────────
def _run_jlink(serial_no, script, timeout=180):
    with tempfile.NamedTemporaryFile("w", suffix=".jlink", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        # stdin 은 막는다 — 스크립트가 어떤 이유로 일찍 끝나도 대화형으로 떨어져 사람 앞에서
        # 멈추는 일이 없어야 한다(connected_jlinks 주석 참조).
        return subprocess.run(
            [JLINK_EXE, "-nogui", "1", "-autoconnect", "1", "-device", DEVICE,
             "-if", "SWD", "-speed", SPEED, "-SelectEmuBySN", str(serial_no),
             "-CommanderScript", path],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    finally:
        os.unlink(path)


def read_words(serial_no, addr, count):
    try:
        p = _run_jlink(serial_no, "mem32 %#010x %x\nq\n" % (addr, count), timeout=60)
    except Exception:
        return None
    words = []
    for line in p.stdout.splitlines():
        m = re.match(r"^([0-9A-F]{8}) = (.+)$", line.strip())
        if m:
            words += [int(x, 16) for x in m.group(2).split()]
    return words[:count] if words else None


def read_label(serial_no):
    return id_unpack(read_words(serial_no, UICR_ID_BASE, ID_WORDS))


# Finder 에서 그냥 여는 파일. json 도 읽는다(옛 경로 호환).
LEDGER_PATH = os.environ.get("WB_FLASH_LEDGER") or os.path.join(THIS_DIR, "flash_ledger.yaml")

# 무선 주소. 0=미기록, 0xFF=ANY(프로토콜). 한 도메인에 1–254.
WB_BEACON_ID_MIN = 1
WB_BEACON_ID_MAX = 254
WB_BEACON_ID_ANY = 255

_LEDGER_COMMENT = (
    "flash.py 원장. 키는 케이스 SERIAL 이다 — beacon:N 으로 적으면 같은 번호를 "
    "다른 보드에 굽는 순간 옛 보드가 지워진다. SSOT 는 UICR. 실물과 다르면 기기가 맞다."
)


def _serial_key(serial):
    return str(int(serial))


def migrate_ledger(led):
    """옛 `beacon:1` 키를 by_serial 로 옮긴다. 같은 SERIAL 이 있으면 그 줄을 지워지 않는다."""
    if not isinstance(led, dict):
        led = {}
    out = {"_comment": led.get("_comment") or _LEDGER_COMMENT,
           "by_serial": dict(led.get("by_serial") or {})}
    for k, v in led.items():
        if k.startswith("_") or k == "by_serial" or not isinstance(v, dict):
            continue
        ser = v.get("serial")
        if not ser:
            continue
        sk = _serial_key(ser)
        if sk in out["by_serial"]:
            continue
        rec = dict(v)
        rec["serial"] = int(ser)
        if ":" in k:
            role, _, n = k.partition(":")
            rec.setdefault("role", role)
            if n.isdigit() and rec.get("id") is None:
                rec["id"] = int(n)
        else:
            rec.setdefault("role", k)
        out["by_serial"][sk] = rec
    return out


def _ledger_candidates(path):
    """지정 경로 + 확장자만 다른 옛 파일."""
    out = [path]
    root, ext = os.path.splitext(path)
    other = ".json" if ext.lower() in (".yaml", ".yml") else ".yaml"
    alt = root + other
    if alt not in out:
        out.append(alt)
    return out


def _read_ledger_file(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        return json.loads(text or "{}")
    import yaml
    return yaml.safe_load(text) or {}


def load_ledger():
    raw = {}
    for p in _ledger_candidates(LEDGER_PATH):
        if not os.path.exists(p):
            continue
        try:
            raw = _read_ledger_file(p)
            break
        except Exception:      # noqa: BLE001 — 깨졌으면 다음 후보
            raw = {}
    return migrate_ledger(raw)


def save_ledger(led, log=print):
    led = migrate_ledger(led)
    led["_comment"] = _LEDGER_COMMENT
    path = LEDGER_PATH
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if path.endswith(".json"):
                json.dump(led, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            else:
                import yaml
                yaml.safe_dump(led, f, allow_unicode=True, sort_keys=True,
                               default_flow_style=False)
        os.replace(tmp, path)
    except OSError as e:
        log("    (기록 실패: %s — 굽기 자체는 끝났다)" % e)


def ledger_used_ids(led, domain, role="beacon"):
    """도메인 안에서 이미 쓰인 id → SERIAL."""
    used = {}
    for v in (migrate_ledger(led).get("by_serial") or {}).values():
        if (v.get("role") or "beacon") != role:
            continue
        if (v.get("domain") or "") != (domain or ""):
            continue
        bid = v.get("id")
        ser = v.get("serial")
        if isinstance(bid, int) and WB_BEACON_ID_MIN <= bid <= WB_BEACON_ID_MAX and ser:
            used[bid] = int(ser)
    return used


def assign_beacon_id(led, serial, domain, requested=None, force=False):
    """이 SERIAL 에 줄 beacon_id.

    이미 원장에 있으면 그 번호를 유지한다. 없으면 도메인 안 빈 번호(1–254).
    requested 가 다른 SERIAL 의 번호면 force 없이 거부.
    반환 (id, err). err 가 있으면 id 는 None.
    """
    if not serial:
        return None, "SERIAL 이 없어 번호를 부여할 수 없다"
    led = migrate_ledger(led)
    used = ledger_used_ids(led, domain)
    mine = (led.get("by_serial") or {}).get(_serial_key(serial)) or {}
    have = mine.get("id") if (mine.get("role") or "beacon") == "beacon" else None
    if not (isinstance(have, int) and WB_BEACON_ID_MIN <= have <= WB_BEACON_ID_MAX):
        have = None

    if requested is None:
        if have is not None:
            return have, None
        for i in range(WB_BEACON_ID_MIN, WB_BEACON_ID_MAX + 1):
            if i not in used:
                return i, None
        return None, "도메인 %s 에 빈 번호가 없다 (1–254)" % (domain or "(없음)")

    requested = int(requested)
    if not (WB_BEACON_ID_MIN <= requested <= WB_BEACON_ID_MAX):
        return None, "beacon_id %s 는 1–254 가 아니다" % requested
    owner = used.get(requested)
    if owner is not None and owner != int(serial) and not force:
        return None, ("%s %d 는 SERIAL %s 가 쓴다. 이 보드(SERIAL %s)에 주려면 --force."
                      % (domain or "도메인", requested, owner, serial))
    return requested, None


def record_flash(role_key, inst, board, rec, app_hex, bootloader_ours, log=print,
                 src="device"):
    """방금 구운 결과를 **SERIAL 키로** 기록한다. 같은 번호를 다른 보드에 굽어도 옛 줄은 남는다."""
    led = load_ledger()
    ser = (rec or {}).get("serial")
    if not ser:
        log("    (기록 생략: SERIAL 없음)")
        return
    rec_out = {
        "serial": int(ser),
        "id": inst,
        "role": role_key,
        "mac": (":".join("%02X" % b for b in rec["label"]) if rec and rec.get("label") else None),
        "domain": (rec or {}).get("domain") or "",
        "board": board,
        "hex": os.path.basename(app_hex) if app_hex else None,
        "bootloader": ("ours" if bootloader_ours else "sdk-fallback"),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": src,
        "beacon_id": (rec or {}).get("beacon_id"),
    }
    led.setdefault("by_serial", {})[_serial_key(ser)] = rec_out
    save_ledger(led, log=log)


def ledger_find_by_serial(serial):
    """이 케이스 SERIAL 을 지난번에 무엇으로 구웠나. (표시키, 항목) 또는 (None, None)."""
    if not serial:
        return None, None
    v = (load_ledger().get("by_serial") or {}).get(_serial_key(serial))
    if not v:
        return None, None
    role = v.get("role") or "beacon"
    inst = v.get("id")
    key = "%s:%d" % (role, inst) if inst else role
    return key, v


def probe_label(serial_no):
    """(레코드|None, 사유). "정보 없음" 으로 뭉치면 새 보드와 깨진 레코드가 구분 안 된다."""
    w = read_words(serial_no, UICR_ID_BASE, ID_WORDS)
    if not w:
        return None, "읽기 실패(보드 미연결?)"
    rec = id_unpack(w)
    if rec:
        return rec, ""
    if w[0] == ID_MAGIC:
        return None, "레코드가 깨졌거나 알 수 없는 버전(v%d)" % ((w[2] >> 16) & 0xFF)
    if all(x == 0xFFFFFFFF for x in w):
        return None, "새 보드(빈 칩)"
    return None, "알 수 없는 내용"


def write_label(serial_no, label, serial, beacon_id, domain, board_type):
    """UICR 에 라벨을 쓴다.
    ⚠ UICR 은 지워진 상태(0xFFFFFFFF)에서만 write 가 먹는다 — mass erase 직후에만 부른다."""
    w = id_pack(label, serial, beacon_id, domain, board_type)
    lines = ["w4 %#010x, %#010x" % (UICR_ID_BASE + 4 * i, w[i]) for i in range(ID_WORDS)]
    try:
        p = _run_jlink(serial_no, "\n".join(lines) + "\nr\nqc\n", timeout=90)
        return p.returncode == 0
    except Exception:
        return False


# ── 굽기 재료 ────────────────────────────────────────────────────────────────
def find_sd_hex():
    for d in (os.path.join(SDK_DIR, "SDK_BSP", "Nordic", "SDK_17_1_0",
                           "components", "softdevice", "s140", "hex"),
              os.path.join(REPO_DIR, "ref")):
        c = sorted(glob.glob(os.path.join(d, "s140_nrf52_*_softdevice.hex")))
        if c:
            return c[-1]
    return None


# 우리 부트로더를 받는 역할 — **비콘만이다.**
#
# ⚠ 라인마다 부트로더가 신뢰하는 **서명키와 광고명이 다르다.** 우리 부트로더는
# WhereVehicle 비콘용(전용 키 + `<도메인>_<SERIAL>` 광고명)이고, 태그는 아직 옛
# 공유키·`WhereB_Tag` 로 도는 부트로더다. 그래서 태그에 우리 것을 구우면 **그 뒤 태그
# OTA 가 통째로 깨진다** — 전송은 붙고 init 패킷에서 `result=0x05` 로 거부된다
# (260820 에 비콘에서 그 증상으로 반나절을 썼다. 겉보기로는 기기 고장처럼 보인다).
# 역할을 안 보고 "우리 것 1순위" 로 고르면 태그를 한 번 굽는 순간 그 사고가 난다.
_OURS_BL_ROLES = ("beacon",)


def find_bootloader_hex(role=None):
    """이 역할에 맞는 부트로더 hex. `role=None` 이면 보수적으로 **SDK 예제**를 준다.

    ⚠ RPi 처럼 SDK 를 안 둔 기기에서도 구워야 한다 — 거기는 `ref/` 에 hex 만 복사해 둔다."""
    sdk = [
        os.path.join(SDK_DIR, "SDK_BSP", "Nordic", "SDK_17_1_0", "examples", "dfu",
                     "secure_bootloader", "pca10100_s140_ble", "armgcc", "_build",
                     "nrf52833_xxaa_s140.hex"),
        os.path.join(REPO_DIR, "ref", "nrf52833_xxaa_s140_bootloader.hex"),
    ]
    ours = os.path.join(REPO_DIR, "firmware", "bootloader", "armgcc", "_build",
                        "nrf52833_xxaa_s140.hex")
    cands = ([ours] + sdk) if (role in _OURS_BL_ROLES) else sdk
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


def find_nrfutil():
    env = os.environ.get("NRFUTIL")
    if env:
        return env
    import shutil as _sh
    p = _sh.which("nrfutil")
    if p:
        return p
    c = os.path.join(THIS_DIR, "nrfutil_venv", "bin", "nrfutil")
    return c if os.path.exists(c) else None


def generate_settings(app_hex, out_path):
    """app hex 의 CRC 로 bootloader settings page 를 만든다.
    ⚠ app 을 바꿀 때마다 다시 만들어야 한다 — 안 맞으면 부트로더가 app 부팅을 거부한다."""
    nrfutil = find_nrfutil()
    if not nrfutil:
        return False, ("nrfutil 없음. `pip install 'nrfutil<7'` 또는 nrfutil+nrf5sdk-tools 설치 후 "
                       "NRFUTIL=<경로> 또는 PATH 노출")
    cmd = [nrfutil, "settings", "generate", "--family", "NRF52",
           "--application", app_hex, "--application-version", "1",
           "--bootloader-version", "1", "--bl-settings-version", "2", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        return False, (r.stderr or r.stdout or "").strip()[:300]
    return True, ""


def load_mapping():
    try:
        with open(DEVICE_MAPPING_FN, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def connected_jlinks():
    """지금 꽂혀 있는 J-Link SN 목록.

    ⚠ **ShowEmuList 를 보내야 목록이 나온다.** 빈 스크립트만 넘기면 아무것도 출력되지 않아
      "연결된 J-Link: (없음)" 이 되는데, 실제로는 4개가 붙어 있었다(260819). 그리고 stdin 을
      막지 않으면 스크립트를 다 읽은 JLinkExe 가 **대화형 프롬프트로 떨어져 사람 앞에서
      멈춘다** — 파이프로 돌리는 곳에서는 EOF 라 안 드러난다.
    """
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".jlink", delete=False) as f:
            f.write("ShowEmuList\nexit\n")
            path = f.name
        try:
            p = subprocess.run([JLINK_EXE, "-nogui", "1", "-CommanderScript", path],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=30)
        finally:
            os.unlink(path)
        out = p.stdout + p.stderr
    except Exception:
        return []
    return sorted(set(re.findall(r"Serial number:\s*(\d+)", out)))


def jlink_table():
    """SN → 사람이 읽는 이름(매핑 기준). cdk 는 사람이 SN 으로 골라야 한다."""
    m = load_mapping()
    names = {}
    for k, v in (m.get("anchors") or {}).items():
        if not k.startswith("_"):
            names[str(v)] = "anchor %s" % k
    if m.get("bo"):
        names[str(m["bo"])] = "bo"
    if m.get("external_debugger"):
        names[str(m["external_debugger"])] = "외장 디버거 (K4W·C3W)"
    return names


# ── 레시피: "무엇을 굽나" ────────────────────────────────────────────────────
#
# 시퀀스는 아래 burn() 한 벌뿐이고, 기기별로 다른 것은 이 표에만 있다.
# 부트로더는 **전 기기 기본**이다 — 예외를 남기면 "이 기기는 어떤 절차더라" 가 다시 생긴다.
# 앵커·BO 도 부트로더를 달면 나중에 원격 업데이트로 갈 수 있고, flash 여유는 45~59KB 남는다.

class Recipe:
    def __init__(self, key, board, hex_path, label_kind, needs_id=False, desc=""):
        self.key = key                  # anchor / beacon / tag / bo / selftest
        self.board = board              # cdk / k4w / c3w
        self.hex_path = hex_path        # {n} 치환 가능
        self.label_kind = label_kind    # BOARD_TYPE_*
        self.needs_id = needs_id        # 인스턴스 번호가 필요한가 (anchor:1, beacon:2)
        self.desc = desc


RECIPES = {
    "anchor":   Recipe("anchor", "cdk", "anchor/output_hex/a{n}.hex",
                       BOARD_TYPE_CDK, True, "차량 앵커 (a1=master)"),
    "beacon":   Recipe("beacon", "k4w", "beacon/output_hex/beacon_k4w.hex",
                       BOARD_TYPE_K4W, True, "배터리 비콘"),
    "tag":      Recipe("tag", "c3w", "tag/output_hex/tag.hex",
                       BOARD_TYPE_C3W, False, "작업자 태그"),
    "bo":       Recipe("bo", "cdk", "beacon_observer/output_hex/bo.hex",
                       BOARD_TYPE_CDK, False, "Beacon Observer (수집기)"),
    "selftest": Recipe("selftest", "k4w", "beacon_selftest/output_hex/selftest_{board}.hex",
                       BOARD_TYPE_UNKNOWN, False, "벤치 자가진단"),
}


def settings_for(app_hex):
    """app 옆에 빌드가 만들어 둔 settings hex. 규약 = `<app>.settings.hex`.

    settings 는 app 의 CRC 로 만드는 것이라 **빌드하는 쪽이 만든다**(build.py: emit()).
    굽는 쪽에서 만들면 굽는 모든 기기에 nrfutil 이 필요해진다."""
    c = app_hex[:-4] + ".settings.hex" if app_hex.endswith(".hex") else app_hex + ".settings.hex"
    return c if os.path.exists(c) else None


def resolve_hex(recipe, inst=None, board=None):
    p = recipe.hex_path.format(n=inst or "", board=board or recipe.board)
    return os.path.join(THIS_DIR, p)


def burn(serial_no, app_hex, *, bootloader=True, keep_label=True, settings_hex=None,
         label=None, serial=0, beacon_id=0, domain="", board_type=BOARD_TYPE_UNKNOWN,
         role=None, dry_run=False, log=print):
    """유일한 굽기 시퀀스.

      recover → SD → [부트로더] → app → [settings] → r/g → [UICR 라벨]

    ⚠ `recover` 로 시작하는 이유 — `erase` 는 lock(APPROTECT)된 칩에서 조용히 실패한다.
      "Downloading" 을 찍고 rc=0 으로 끝나는데 write 가 안 되어 칩이 옛 이미지를 계속
      실행한다. recover(CTRL-AP mass erase)가 먼저 unlock 해야 실제로 써진다.

    ⚠ recover 는 UICR 도 지운다. 그래서 라벨은 **굽고 나서** 복원한다. keep_label=True 면
      굽기 전에 읽어 둔 값을 그대로 다시 쓴다 — 값이 바뀌면 안 되는 자산이기 때문이다.
    """
    sd = find_sd_hex()
    if not sd:
        return False, "SoftDevice hex 를 못 찾았다 (QM33_SDK_DIR 확인)"
    if not os.path.exists(app_hex):
        return False, "app hex 없음: %s" % app_hex

    # 1) 굽기 전에 기존 라벨을 읽어 둔다 (recover 가 지우므로)
    prior = None
    if keep_label:
        prior, why = probe_label(serial_no)
        if prior:
            log("    기존 라벨: %s (%s)" % (label_str(prior), BOARD_TYPE_NAME.get(
                prior.get("board_type", 0), "?")))
        elif why:
            log("    기존 라벨: 없음 — %s" % why)

    steps = ["recover", "loadfile %s" % sd]
    settings_path, made_here = None, False
    if bootloader:
        bl = find_bootloader_hex(role)
        if not os.path.exists(bl):
            return False, "부트로더 hex 없음: %s" % bl
        # [#29] **어느 부트로더가 들어가는지 말한다.** 우리 것은 DFU 주소를 케이스 MAC 에서
        # 만들고 광고명·서명키가 라인별로 갈려 있다. SDK 예제로 폴백하면 그게 전부 사라지는데,
        # 겉으로는 똑같이 "✓ 완료" 라 **조용히 옛 규약으로 되돌아간다.**
        _ours = os.path.join(REPO_DIR, "firmware", "bootloader")
        if os.path.abspath(bl).startswith(os.path.abspath(_ours)):
            log("    부트로더: 우리 것 (케이스 MAC 주소 · 라인별 이름/키)")
        elif role in _OURS_BL_ROLES:
            log("    ⚠ 부트로더: SDK 예제 폴백 — %s" % bl)
            log("       우리 부트로더가 안 빌드돼 있다. 이대로 구우면 DFU 주소가 칩 기본값이 되고")
            log("       광고명·서명키가 옛 공용값이 된다(프로젝트 간 교차 사고의 조건).")
            log("       빌드: cd firmware/bootloader/armgcc && make GNU_INSTALL_ROOT=... GNU_PREFIX=arm-none-eabi")
        else:
            # 이 역할은 SDK 부트로더가 **정답**이다 — 우리 것을 넣으면 그 라인의 OTA 가
            # 깨진다(태그는 아직 옛 키·`WhereB_Tag` 로 돈다).
            log("    부트로더: SDK 예제 (이 역할의 정규 부트로더 — 우리 것은 비콘 전용)")
        settings_hex = settings_hex or settings_for(app_hex)
        if settings_hex:
            # 빌드가 만들어 둔 것(`<app>.settings.hex`) 또는 --settings 로 받은 것.
            if not os.path.exists(settings_hex):
                return False, "settings hex 없음: %s" % settings_hex
            settings_path, made_here = settings_hex, False
            log("    settings: %s" % os.path.basename(settings_path))
        else:
            settings_path, made_here = os.path.join(
                tempfile.gettempdir(), "wb_settings_%s.hex" % os.getpid()), True
            ok, err = generate_settings(app_hex, settings_path)
            if not ok:
                return False, ("settings 가 없고 여기서 만들지도 못했다 — %s\n"
                               "       app 을 빌드한 곳에서 `python3 build.py <role>` 을 다시 돌리면\n"
                               "       `<app>.settings.hex` 가 함께 생긴다(그 파일도 같이 전송할 것)" % err)
        steps += ["loadfile %s" % bl, "loadfile %s" % app_hex,
                  "loadfile %s" % settings_path]
    else:
        steps += ["loadfile %s" % app_hex]
    steps += ["r", "g", "qc"]

    if dry_run:
        log("    [dry-run] " + " ; ".join(steps))
        return True, "dry-run"

    p = _run_jlink(serial_no, "\n".join(steps) + "\n")
    out = p.stdout + p.stderr
    if settings_path and made_here and os.path.exists(settings_path):
        os.unlink(settings_path)
    if p.returncode != 0 or "Program & Verify" not in out:
        tail = [l for l in out.splitlines()
                if any(k in l for k in ("ERROR", "Cannot", "not found", "FAILED"))]
        msg = (tail[-1].strip() if tail else "flash 실패 (rc=%d)" % p.returncode)
        # 가장 흔한 원인이 "다른 JLink 프로세스가 프로브를 쥐고 있다" 인데, 메시지만 보면
        # 케이블·보드를 의심하게 된다. 실제로 RTT 로거·GUI 가 남아 있던 경우가 대부분이다.
        if "Cannot connect" in msg or "FAILED" in msg:
            msg += ("\n       ↳ 프로브를 다른 프로세스가 쥐고 있을 수 있다: "
                    "`pkill -f JLink` 후 재시도 (RTT 로거·JLinkConfig GUI 가 흔한 범인)")
        return False, msg

    # 2) 라벨 복원 또는 신규 기록
    if keep_label and prior:
        # beacon_id 인자를 주면 복원 때 번호를 바꾼다(회사 1–4 와 겹친 현장 보드 재부여).
        bid = beacon_id if beacon_id else prior.get("beacon_id") or 0
        dom = domain or prior.get("domain") or ""
        write_label(serial_no, prior["label"], prior["serial"], bid,
                    dom, prior["board_type"] or board_type)
        if bid != prior.get("beacon_id"):
            log("    라벨 복원 · id %s→%s: %s" % (
                prior.get("beacon_id"), bid, label_str(prior)))
        else:
            log("    라벨 복원: %s" % label_str(prior))
    elif label is not None:
        write_label(serial_no, label, serial, beacon_id, domain, board_type)
        log("    라벨 기록: %s" % label_str(
            {"label": label, "serial": serial, "board_type": board_type}))
    return True, ""


# ── 라벨 입력 (신규 보드) ────────────────────────────────────────────────────
#
# 케이스에 인쇄된 값을 사람이 넣어 준다. 형식은 board_type 마다 다르다.
#   K4W·C3W : QR "MAC:BC5729191FB2,SERIAL:596574;" 또는 손입력 (MAC + SERIAL)
#   CDK     : 보드에 인쇄된 10자리 hex SN (예 A1B2C3D4E5) → label = 00 + 그 5바이트
#
# ⚠ PC 에는 QR 스캐너가 없다 — QR 형식을 강요하면 일을 두 번 하게 되므로 손입력도 받는다.

def parse_mac_serial(text):
    """QR 문자열 또는 손입력 → (label[6], serial). 못 읽으면 None."""
    t = (text or "").strip().rstrip(";")
    if not t:
        return None
    mac_hex = ser = None
    if ":" in t and ("MAC" in t.upper() or "SERIAL" in t.upper()):
        for part in t.replace(";", ",").split(","):
            if not part.strip():
                continue
            k, _, v = part.partition(":")
            k = k.strip().upper()
            if k == "MAC":
                mac_hex = v.strip()
            elif k in ("SERIAL", "SN"):
                ser = v.strip()
    if not mac_hex or not ser:
        return None
    h = mac_hex.replace(":", "").replace("-", "").strip()
    if len(h) != 12:
        return None
    try:
        return [int(h[i:i + 2], 16) for i in range(0, 12, 2)], int(str(ser).strip())
    except ValueError:
        return None


def parse_printed_sn(text):
    """CDK 보드에 인쇄된 10자리 hex SN → label[6] (`00` + 5바이트). 못 읽으면 None.

    ⚠ J-Link SN(10진수)과 헷갈리기 쉽다 — 이건 **보드에 인쇄된 hex** 다."""
    h = (text or "").strip().replace(":", "").replace("-", "").replace("0x", "").upper()
    if len(h) != 10 or any(c not in "0123456789ABCDEF" for c in h):
        return None
    return [0x00] + [int(h[i:i + 2], 16) for i in range(0, 10, 2)]


def known_domains():
    """build_config.beacon.domains (첫 항목이 기본값)."""
    try:
        import yaml
        with open(os.path.join(THIS_DIR, "build_config.yaml"), encoding="utf-8") as f:
            d = (yaml.safe_load(f).get("beacon") or {}).get("domains") or []
        return [str(x).strip() for x in d if str(x).strip()]
    except Exception:
        return []


def prompt_label(board_type, log=print, ask=input):
    """신규 보드의 라벨을 받는다. (label, serial) 또는 (None, 0).

    ⚠ 도메인은 대소문자가 다르면 **다른 도메인**이다(기기에 구워지는 값).
    """
    if board_type == BOARD_TYPE_CDK:
        log("    이 보드에는 라벨이 없습니다. 보드에 인쇄된 **10자리 hex SN** 을 입력하세요.")
        log("    (J-Link SN 이 아닙니다 — 보드에 적힌 값)")
        for _ in range(3):
            v = ask("    SN> ").strip()
            if not v:
                return None, 0
            lab = parse_printed_sn(v)
            if lab:
                return lab, 0
            log("    10자리 hex 가 아닙니다. 예: A1B2C3D4E5")
        return None, 0
    log("    이 보드에는 케이스 정보가 없습니다. 입력해 주세요.")
    log("    (QR 스캔 결과를 그대로 붙여넣거나, MAC 과 SERIAL 을 따로)")
    for _ in range(3):
        v = ask("    QR> ").strip()
        if not v:
            break
        got = parse_mac_serial(v)
        if got:
            return got
        log("    형식을 못 읽었습니다. 예: MAC:BC5729191FB2,SERIAL:596574;")
    mac = ask("    MAC (BC:57:29:19:1F:B2): ").strip()
    ser = ask("    SERIAL (596574): ").strip()
    got = parse_mac_serial("MAC:%s,SERIAL:%s" % (mac, ser))
    return got if got else (None, 0)


# ── 낡은 hex 굽기 방지 ───────────────────────────────────────────────────────
#
# "코드는 고쳤는데 동작이 그대로" 의 진범이 이것이었다 — 굽는 도구가 산출물이 있으면
# 빌드를 건너뛰어 몇 시간 전 hex 를 구웠고, flash 로그는 전 구간 O.K. 라 단서가 없었다.
# 빌드 시스템을 흉내내는 게 아니라 **"이 hex 를 믿어도 되나" 만 판정**한다.
# 놓치면 조용한 사고, 과하게 잡으면 불필요한 재빌드일 뿐이라 의심 쪽으로 기운다.

_SRC_ROOTS = ("beacon/Src", "beacon/Inc", "beacon_common", "shared",
              "anchor/Src", "tag/Src", "beacon_observer/Src")


def newest_source_mtime():
    newest = 0.0
    for r in _SRC_ROOTS:
        for dirpath, _dirs, files in os.walk(os.path.join(THIS_DIR, r)):
            for f in files:
                if f.endswith((".c", ".h", ".ld", ".yaml")):
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
                    except OSError:
                        pass
    for f in ("build_config.yaml", "build.py"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(THIS_DIR, f)))
        except OSError:
            pass
    return newest


def stale_reason(path):
    """굽기 전에 재빌드가 필요한 이유(없으면 None) — 사람이 읽을 한 줄."""
    if not os.path.exists(path):
        return "hex 가 없습니다: %s" % os.path.basename(path)
    src = newest_source_mtime()
    if src > os.path.getmtime(path):
        import datetime as _dt
        fmt = lambda t: _dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
        return ("소스가 hex 보다 최신입니다 (hex %s < 소스 %s) — 다시 빌드하세요"
                % (fmt(os.path.getmtime(path)), fmt(src)))
    return None


def remember_domain(name):
    """새 도메인을 build_config.yaml 의 인라인 리스트에 덧붙인다.

    PyYAML 로 다시 쓰면 **주석이 통째로 날아간다** — 이 파일은 "이 빌드가 무엇이었나" 를
    설명하는 주석이 값만큼 중요하다. 그래서 그 한 줄만 텍스트로 고친다.
    """
    cfg_path = os.path.join(THIS_DIR, "build_config.yaml")
    try:
        src = open(cfg_path, encoding="utf-8").read()
        m = re.search(r"^(\s*domains:\s*)\[([^\]]*)\]", src, re.M)
        if not m:
            return False
        items = [x.strip() for x in m.group(2).split(",") if x.strip()]
        if name in items:
            return True
        items.append(name)
        src = src[:m.start()] + f"{m.group(1)}[{', '.join(items)}]" + src[m.end():]
        open(cfg_path, "w", encoding="utf-8").write(src)
        return True
    except Exception:
        return False
