"""
streamlit_app.py - 거래량 대시보드 v7
v6에서 추가/변경:
- 신규 멤버 추가하기: 이미 있는 UID면 자동 병합/업그레이드 (중복 행 방지)
  - 예: 지표만 + 폼에서 트레이딩룸만 체크 → 트레이딩룸+지표로 업그레이드
- 중복 UID 스캔 기능 (구조 문제 있는 UID 찾아냄)
- 표시 메시지 개선 (신규/업그레이드/변경없음/에러 명확히 구분)
"""

import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials


VOLUME_THRESHOLD = 50_000
PERIOD_LABEL = "이번 달"
OKX_API_BASE = "https://www.okx.com"

# 통합 관리대장
SHEET_ID = "1kahCWtaZ35pbCa7XMBcrPNEXsGTQ28XTCTYA8qmL-uY"
SHEET_TAB = "통합 관리대장"

# 시트 컬럼 (1-based, A=1)
COL_UID = 1        # A
COL_NICK = 2       # B
COL_GUBUN = 3      # C
COL_ROOM_DATE = 4  # D
COL_SEED = 5       # E
COL_IND_DATE = 6   # F
COL_CONFIRMED = 7  # G
COL_EXCLUDED = 8   # H
COL_MEMO = 9       # I
COL_LAST_30D_VOL = 10       # J
COL_LAST_30D_UPDATED = 11   # K

LEDGER_COLS = [
    "트뷰 닉네임", "구분", "트레이딩룸 입장일", "입장시드",
    "지표 지급날짜", "실제 입장확인", "제외일", "메모",
    "최근 30일 거래량", "갱신일시",
]
GUBUN_OPTIONS = ["트레이딩룸+지표", "트레이딩룸만", "지표만", "제외"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
KST = timezone(timedelta(hours=9))

# 우리 소속으로 인정할 어필리에이트 코드 목록
# (K2110 같은 하위 인플루언서 코드는 여기 넣지 않음)
OUR_AFFILIATE_CODES = {"41888826", "BULDAN", "DANTARANG"}


def _today_str():
    now = datetime.now(KST)
    return f"{now.year}. {now.month}. {now.day}"


def merge_gubun(existing, adding):
    """기존 구분 + 폼에서 새로 체크한 구분 → 최종 구분."""
    has_room = "트레이딩룸" in existing or "트레이딩룸" in adding
    has_ind = "지표" in existing or "지표" in adding
    if has_room and has_ind:
        return "트레이딩룸+지표"
    if has_room:
        return "트레이딩룸만"
    if has_ind:
        return "지표만"
    return existing


# ═══════════════════════════════════════════
# 비밀번호 잠금
# ═══════════════════════════════════════════

def check_password():
    def _on_change():
        if st.session_state.get("_pwd_input") == st.secrets.get("DASHBOARD_PASSWORD"):
            st.session_state["_pwd_ok"] = True
        else:
            st.session_state["_pwd_ok"] = False

    if st.session_state.get("_pwd_ok"):
        return True

    st.title("🔒 거래량 관리")
    st.text_input("비밀번호", type="password", key="_pwd_input", on_change=_on_change)
    if st.session_state.get("_pwd_ok") is False:
        st.error("비밀번호가 틀려요")
    return False


# ═══════════════════════════════════════════
# OKX
# ═══════════════════════════════════════════

def load_accounts():
    accounts = []
    try:
        a = st.secrets["okx_a"]
        accounts.append({
            "name": "계정A", "api_key": a["api_key"],
            "secret_key": a["secret_key"], "passphrase": a["passphrase"],
        })
    except Exception:
        pass
    try:
        b = st.secrets["okx_b"]
        accounts.append({
            "name": "계정B", "api_key": b["api_key"],
            "secret_key": b["secret_key"], "passphrase": b["passphrase"],
        })
    except Exception:
        pass
    return accounts


def _signature(timestamp, method, path, secret):
    message = timestamp + method + path
    mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _check_uid(uid, account):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"
    sig = _signature(timestamp, "GET", path, account["secret_key"])
    headers = {
        "OK-ACCESS-KEY": account["api_key"],
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": account["passphrase"],
    }
    try:
        res = requests.get(OKX_API_BASE + path, headers=headers, timeout=10)
        return res.json()
    except requests.exceptions.Timeout:
        return {"code": "error", "msg": "OKX API 타임아웃"}
    except requests.exceptions.RequestException as e:
        return {"code": "error", "msg": f"요청 실패: {e}"}
    except ValueError as e:
        return {"code": "error", "msg": f"응답 파싱 실패: {e}"}


def call_invitee_list_page(account, page=1, limit=100):
    """/list 엔드포인트 한 페이지 뽑기 (last_30d 기준)."""
    from urllib.parse import urlencode
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    query = urlencode({
        "limit": str(limit),
        "page": str(page),
        "periodType": "last_30d",
    })
    path = f"/api/v5/affiliate/invitee/list?{query}"
    sig = _signature(timestamp, "GET", path, account["secret_key"])
    headers = {
        "OK-ACCESS-KEY": account["api_key"],
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": account["passphrase"],
    }
    try:
        res = requests.get(OKX_API_BASE + path, headers=headers, timeout=15)
        return res.json()
    except requests.exceptions.Timeout:
        return {"code": "error", "msg": "timeout"}
    except Exception as e:
        return {"code": "error", "msg": str(e)}


def _safe_float(value, default=0.0):
    try:
        return float(value) if value else default
    except (ValueError, TypeError):
        return default


def lookup_member(uid, accounts):
    if not uid or not uid.isdigit():
        return {"found": False, "error": "숫자가 아님"}
    if not (15 <= len(uid) <= 18):
        return {"found": False, "error": f"{len(uid)}자리(비정상)"}

    for account in accounts:
        result = _check_uid(uid, account)
        if result.get("code") == "0" and result.get("data"):
            data = result["data"][0]
            aff_code = data.get("affiliateCode", "")
            if aff_code in OUR_AFFILIATE_CODES:
                return {
                    "found": True, "error": None,
                    "month_volume": _safe_float(data.get("volMonth")),
                    "total_volume": _safe_float(data.get("totalTradingVolume")),
                    "total_commission": _safe_float(data.get("totalCommission")),
                    "deposit": _safe_float(data.get("depAmt")),
                    "account": account["name"],
                    "affiliate_code": aff_code,
                }
    return {"found": False, "error": "가입자 아님"}


# ═══════════════════════════════════════════
# 구글시트 (gspread)
# ═══════════════════════════════════════════

@st.cache_resource(show_spinner=False)
def get_worksheet():
    creds_dict = json.loads(st.secrets["GOOGLE_CREDENTIALS_JSON"])
    credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(credentials)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_TAB)


@st.cache_data(ttl=300, show_spinner=False)
def load_ledger():
    try:
        ws = get_worksheet()
        all_values = ws.get_all_values()
        if not all_values:
            return {}, "시트가 비어있어요."
        header = [h.strip() for h in all_values[0]]
        if "UID" not in header:
            return {}, f"시트에 UID 칸이 없어요. (찾은 칸: {header})"
        uid_idx = header.index("UID")
        col_indexes = {c: header.index(c) for c in LEDGER_COLS if c in header}

        ledger = {}
        for row in all_values[1:]:
            if uid_idx >= len(row):
                continue
            uid = str(row[uid_idx]).strip()
            if not uid:
                continue
            ledger[uid] = {
                col: str(row[idx]).strip() if idx < len(row) else ""
                for col, idx in col_indexes.items()
            }
        return ledger, None
    except Exception as e:
        return {}, f"통합대장 불러오기 실패: {e}"


def build_uid_row_map(ws):
    col_values = ws.col_values(COL_UID)
    return {
        str(v).strip(): i for i, v in enumerate(col_values, start=1)
        if str(v).strip()
    }


def append_new_member(ws, uid, nickname, gubun, seed, memo):
    """신규 UID: 새 행으로 추가. 구분에 따라 D/F/H 자동."""
    today = _today_str()
    room_date = today if "트레이딩룸" in gubun else ""
    ind_date = today if "지표" in gubun else ""
    excl_date = today if gubun == "제외" else ""
    new_row = [uid, nickname, gubun, room_date, seed, ind_date, "", excl_date, memo]
    ws.append_row(new_row, value_input_option="USER_ENTERED")


def upgrade_existing_member(ws, uid, existing, adding_gubun,
                            nickname_input, seed_input, memo_input):
    """
    기존 UID: 필요한 칸만 업데이트.
    - 폼 입력값 있으면 덮어쓰기, 비었으면 기존 유지
    - 구분은 병합 규칙에 따라 계산
    - D/F: 비어있고 필요하면 오늘 날짜 자동
    Returns: (status, before_gubun, after_gubun)
      status: "upgraded" | "no_change"
    """
    existing_gubun = existing.get("구분", "")
    final_gubun = merge_gubun(existing_gubun, adding_gubun)

    col_updates = []

    # 구분 변경
    if final_gubun != existing_gubun:
        col_updates.append((COL_GUBUN, final_gubun))

    # 닉네임: 입력값 있으면 덮어쓰기
    if nickname_input.strip():
        if nickname_input.strip() != existing.get("트뷰 닉네임", "").strip():
            col_updates.append((COL_NICK, nickname_input.strip()))

    # 시드: 입력값 있으면 덮어쓰기
    if seed_input.strip():
        if seed_input.strip() != existing.get("입장시드", "").strip():
            col_updates.append((COL_SEED, seed_input.strip()))

    # 메모: 입력값 있으면 덮어쓰기
    if memo_input.strip():
        if memo_input.strip() != existing.get("메모", "").strip():
            col_updates.append((COL_MEMO, memo_input.strip()))

    # 날짜 자동: 비어있을 때만
    today = _today_str()
    if "트레이딩룸" in final_gubun and not existing.get("트레이딩룸 입장일", "").strip():
        col_updates.append((COL_ROOM_DATE, today))
    if "지표" in final_gubun and not existing.get("지표 지급날짜", "").strip():
        col_updates.append((COL_IND_DATE, today))

    if not col_updates:
        return "no_change", existing_gubun, final_gubun

    # 시트 반영
    uid_to_row = build_uid_row_map(ws)
    row = uid_to_row.get(str(uid))
    if row is None:
        raise RuntimeError(f"시트에서 UID {uid} 행을 찾지 못함 (이론상 불가능)")

    batch = []
    for col_idx, val in col_updates:
        cell = gspread.utils.rowcol_to_a1(row, col_idx)
        batch.append({"range": cell, "values": [[val]]})
    ws.batch_update(batch)
    return "upgraded", existing_gubun, final_gubun


def apply_edits_batch(ws, edits):
    uid_to_row = build_uid_row_map(ws)
    batch = []
    unfound = []
    for edit in edits:
        row = uid_to_row.get(str(edit["uid"]))
        if row is None:
            unfound.append(edit["uid"])
            continue
        for col_idx, val in edit["col_updates"]:
            cell = gspread.utils.rowcol_to_a1(row, col_idx)
            batch.append({"range": cell, "values": [[val]]})
    if batch:
        ws.batch_update(batch)
    return len(batch), unfound


def mark_confirmed_batch(ws, uids):
    uid_to_row = build_uid_row_map(ws)
    success, fail, updates = [], [], []
    for uid in uids:
        row = uid_to_row.get(str(uid))
        if row is None:
            fail.append(uid)
            continue
        cell = gspread.utils.rowcol_to_a1(row, COL_CONFIRMED)
        updates.append({"range": cell, "values": [["확인"]]})
        success.append(uid)
    if updates:
        ws.batch_update(updates)
    return success, fail


def scan_duplicates(ws):
    """시트 훑어서 UID 중복된 것 찾아 반환. [{uid, rows:[(sheet_row, gubun, nick), ...]}]"""
    all_values = ws.get_all_values()
    if not all_values:
        return []
    header = [h.strip() for h in all_values[0]]
    try:
        uid_idx = header.index("UID")
        gubun_idx = header.index("구분")
        nick_idx = header.index("트뷰 닉네임")
    except ValueError:
        return []

    seen = {}
    for i, row in enumerate(all_values[1:], start=2):
        if uid_idx >= len(row):
            continue
        uid = str(row[uid_idx]).strip()
        if not uid:
            continue
        gubun = str(row[gubun_idx]).strip() if gubun_idx < len(row) else ""
        nick = str(row[nick_idx]).strip() if nick_idx < len(row) else ""
        seen.setdefault(uid, []).append(
            {"sheet_row": i, "구분": gubun, "닉네임": nick}
        )

    return [
        {"uid": uid, "rows": rows}
        for uid, rows in seen.items() if len(rows) > 1
    ]


def _save_last_30d_chunk(ws, matched_map, uid_to_row, now_str):
    """매칭된 UID들의 최근 30일 거래량·갱신일시 배치 저장."""
    updates = []
    for uid, vol in matched_map.items():
        row = uid_to_row.get(uid)
        if row is None:
            continue
        vol_cell = gspread.utils.rowcol_to_a1(row, COL_LAST_30D_VOL)
        upd_cell = gspread.utils.rowcol_to_a1(row, COL_LAST_30D_UPDATED)
        updates.append({"range": vol_cell, "values": [[f"{vol:.2f}"]]})
        updates.append({"range": upd_cell, "values": [[now_str]]})
    if updates:
        ws.batch_update(updates)


def scan_last_30d_volumes(accounts, target_uids, ws, chunk_pages=100,
                          progress_bar=None, status_area=None):
    """
    전체 회원 스캔 후 target_uids 매칭 → 시트 J/K열 저장.
    페이지 청크마다 즉시 저장 (안전장치).
    """
    import time as _time
    now_str = datetime.now(KST).strftime("%Y. %m. %d %H:%M")
    uid_to_row = build_uid_row_map(ws)

    # 각 계정별 총 페이지 수 파악 (page=1 호출)
    account_pages = []
    for acc in accounts:
        first = call_invitee_list_page(acc, page=1, limit=100)
        if first.get("code") != "0":
            if status_area:
                status_area.warning(
                    f"{acc['name']} 첫 페이지 호출 실패: {first.get('msg')}"
                )
            account_pages.append((acc, 0, None))
            continue
        total = int(first.get("totalPage", 1))
        account_pages.append((acc, total, first))

    total_pages_all = sum(p for _, p, _ in account_pages)
    if total_pages_all == 0:
        return {"matched": 0, "pages_done": 0, "failed_pages": []}

    matched_map = {}      # uid -> volume (최신값 유지)
    pending = {}          # 저장 대기 (청크 저장용)
    failed_pages = []
    pages_done = 0
    start = _time.time()

    for acc, total, first in account_pages:
        if total == 0:
            continue

        # 첫 페이지 처리 (이미 받아둔 응답)
        pages_to_iter = [(1, first)]
        for page in range(2, total + 1):
            pages_to_iter.append((page, None))

        for page_num, cached_resp in pages_to_iter:
            resp = cached_resp if cached_resp is not None else \
                call_invitee_list_page(acc, page=page_num, limit=100)

            if resp.get("code") == "0":
                for r in resp.get("data", []):
                    uid = str(r.get("uid", "")).strip()
                    if uid in target_uids:
                        vol = _safe_float(r.get("totalVol"))
                        matched_map[uid] = vol
                        pending[uid] = vol
            else:
                failed_pages.append((acc["name"], page_num))

            pages_done += 1

            # 진행률 갱신 (매 5페이지)
            if pages_done % 5 == 0 or pages_done == total_pages_all:
                pct = pages_done / total_pages_all
                elapsed = _time.time() - start
                eta = (elapsed / pct - elapsed) if pct > 0 else 0
                msg = (f"📡 {acc['name']} · 페이지 {pages_done}/{total_pages_all} "
                       f"· 매칭 {len(matched_map)}명 "
                       f"· 경과 {int(elapsed)}초 · 남음 약 {int(eta)}초")
                if progress_bar is not None:
                    progress_bar.progress(pct, text=msg)
                if status_area is not None:
                    status_area.markdown(msg)

            # 청크 단위로 시트 저장
            if pages_done % chunk_pages == 0 and pending:
                try:
                    _save_last_30d_chunk(ws, pending, uid_to_row, now_str)
                    pending = {}
                except Exception as e:
                    if status_area:
                        status_area.warning(f"중간 저장 실패 (재시도 예정): {e}")

            _time.sleep(0.05)  # rate limit 완화

    # 남은 것 저장
    if pending:
        _save_last_30d_chunk(ws, pending, uid_to_row, now_str)

    return {
        "matched": len(matched_map),
        "pages_done": pages_done,
        "failed_pages": failed_pages,
        "updated_at": now_str,
    }


# ═══════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════

st.set_page_config(page_title="거래량 관리", page_icon="📊", layout="wide")

if not check_password():
    st.stop()

st.title("📊 트레이딩룸 거래량 현황")
st.caption(f"기준: {PERIOD_LABEL} 거래량 ${VOLUME_THRESHOLD:,} 이상")

accounts = load_accounts()
if not accounts:
    st.error("OKX 계정 정보가 아직 설정되지 않았어요. Settings → Secrets 확인")
    st.stop()

ledger, ledger_err = load_ledger()
col_info, col_btn = st.columns([5, 1])
with col_info:
    if ledger_err:
        st.warning(f"⚠️ 통합 관리대장: {ledger_err}")
    else:
        st.success(f"📒 통합 관리대장 연결됨 — {len(ledger)}명 (5분 캐시)")
with col_btn:
    if st.button("🔄 대장 새로고침"):
        load_ledger.clear()
        st.rerun()

# ─── 📡 최근 30일 거래량 스캔 ───
# 스캔 대상: 통합대장에서 트레이딩룸 관련 UID만 매칭
_today_ymd = _today_str()
scan_target_uids = {
    uid for uid, info in ledger.items()
    if info.get("구분") in ("트레이딩룸+지표", "트레이딩룸만")
}
_updated_dates = [
    info.get("갱신일시", "").split(" ")[0]
    for uid, info in ledger.items()
    if uid in scan_target_uids and info.get("갱신일시", "").strip()
]
_scanned_today = sum(1 for d in _updated_dates if d == _today_ymd)
_last_updates = sorted([d for d in _updated_dates if d], reverse=True)
_last_scan = _last_updates[0] if _last_updates else "없음"

with st.expander(f"📡 최근 30일 거래량 스캔 — 마지막 갱신: {_last_scan} "
                 f"(오늘 갱신 {_scanned_today}/{len(scan_target_uids)})"):
    st.caption(
        f"트레이딩룸 회원 **{len(scan_target_uids)}명**의 최근 30일 거래량을 "
        f"OKX에서 전체 스캔해서 시트 J열/K열에 저장합니다. "
        f"7~13분 정도 걸리며, 100페이지마다 시트에 즉시 저장되므로 중간에 끊겨도 그때까지 데이터는 남습니다. "
        f"진행 중에는 이 창을 닫지 마세요."
    )

    if st.button("🔄 지금 스캔", type="primary", key="scan_btn"):
        if not scan_target_uids:
            st.warning("트레이딩룸 회원이 없어 스캔할 대상이 없어요.")
        else:
            try:
                ws = get_worksheet()
                progress_bar = st.progress(0.0, text="준비 중...")
                status_area = st.empty()

                result = scan_last_30d_volumes(
                    accounts, scan_target_uids, ws,
                    chunk_pages=100,
                    progress_bar=progress_bar,
                    status_area=status_area,
                )

                progress_bar.progress(1.0, text="✅ 스캔 완료")
                load_ledger.clear()
                st.success(
                    f"✅ 스캔 완료 · 매칭 {result['matched']}명 저장 · "
                    f"페이지 {result['pages_done']}개 처리 · "
                    f"실패 페이지 {len(result['failed_pages'])}개"
                )
                if result["failed_pages"]:
                    st.warning(
                        f"실패한 페이지: {result['failed_pages'][:20]}..."
                        if len(result["failed_pages"]) > 20
                        else f"실패한 페이지: {result['failed_pages']}"
                    )
                    st.caption("실패 페이지가 있으면 다시 스캔하면 이어서 채워집니다.")
            except Exception as e:
                st.error(f"스캔 실패: {e}")


# ─── 신규 멤버 추가 / 업그레이드 ───
with st.expander("➕ 신규 멤버 추가하기 / 기존 멤버 업그레이드"):
    with st.form("add_member_form", clear_on_submit=True):
        st.caption(
            "이미 있는 UID면 자동으로 정보가 병합/업그레이드됩니다. "
            "(예: 지표만 → 트레이딩룸만 체크 → 트레이딩룸+지표로 업그레이드)"
        )
        nm_uid = st.text_input("UID *")
        nm_nick = st.text_input("트뷰 닉네임")
        nm_gubun = st.selectbox("구분 *", GUBUN_OPTIONS)
        nm_seed = st.text_input("입장시드 (트레이딩룸 관련일 때만)")
        nm_memo = st.text_input("메모")
        submitted = st.form_submit_button("✅ 통합대장에 반영", type="primary")

    if submitted:
        uid_clean = nm_uid.strip()
        if not uid_clean:
            st.error("UID를 입력하세요.")
        elif not uid_clean.isdigit():
            st.error("UID는 숫자만 가능해요.")
        elif not (15 <= len(uid_clean) <= 18):
            st.error(f"UID는 15~18자리여야 해요. (현재 {len(uid_clean)}자리)")
        else:
            try:
                ws = get_worksheet()
                if uid_clean in ledger:
                    existing = ledger[uid_clean]
                    existing_gubun = existing.get("구분", "")

                    if existing_gubun == "제외":
                        st.error(
                            f"❌ 이 UID는 현재 '제외' 상태예요. "
                            f"재등록 기능은 아직 미지원입니다. "
                            f"필요하면 시트에서 직접 정리해주세요."
                        )
                    else:
                        status, before, after = upgrade_existing_member(
                            ws, uid_clean, existing, nm_gubun,
                            nm_nick, nm_seed, nm_memo
                        )
                        load_ledger.clear()
                        if status == "no_change":
                            st.info(
                                f"ℹ️ 이미 '{after}' 상태이고 폼에 새 정보도 없어요. "
                                f"변경사항 없음."
                            )
                        elif before == after:
                            # 구분은 그대로지만 다른 칸(닉네임/시드/메모)만 갱신된 경우
                            st.success(
                                f"✅ {uid_clean} 정보 업데이트 완료 (구분: {after} 유지)"
                            )
                        else:
                            st.success(
                                f"✅ {uid_clean} 업그레이드 완료: "
                                f"**{before}** → **{after}**"
                            )
                else:
                    append_new_member(
                        ws, uid_clean, nm_nick.strip(), nm_gubun,
                        nm_seed.strip(), nm_memo.strip()
                    )
                    load_ledger.clear()
                    st.success(
                        f"✅ 신규 등록 완료: {uid_clean} (구분: **{nm_gubun}**)"
                    )
            except Exception as e:
                st.error(f"처리 실패: {e}")

# ─── 중복 UID 스캔 ───
with st.expander("🔍 중복 UID 스캔"):
    st.caption(
        "시트에 같은 UID가 여러 행으로 흩어져 있는 경우 찾아냅니다. "
        "발견 시 시트에서 직접 정리하세요."
    )
    if st.button("스캔 시작"):
        try:
            with st.spinner("시트 훑는 중..."):
                ws = get_worksheet()
                dups = scan_duplicates(ws)
            if not dups:
                st.success("✅ 중복 UID 없음. 시트가 깨끗해요.")
            else:
                st.warning(f"⚠️ 중복 UID **{len(dups)}개** 발견")
                rows = []
                for d in dups:
                    for r in d["rows"]:
                        rows.append({
                            "UID": d["uid"],
                            "시트 행 번호": r["sheet_row"],
                            "구분": r["구분"] or "-",
                            "닉네임": r["닉네임"] or "-",
                        })
                st.dataframe(pd.DataFrame(rows), use_container_width=True,
                             hide_index=True)
                st.caption(
                    "👆 각 UID를 시트에서 검색해서 정리해주세요. "
                    "보통 두 행 중 하나만 남기고 다른 하나에서 필요한 정보를 옮기는 방식."
                )
        except Exception as e:
            st.error(f"스캔 실패: {e}")

# ─── UID 조회 ───
st.subheader("🔍 UID 조회")
st.write("확인할 UID를 한 줄에 하나씩 붙여넣으세요.")

uid_text = st.text_area("UID 목록", value="", height=200,
                        placeholder="123456789012345\n234567890123456",
                        key="uid_input")

if st.button("조회", type="primary"):
    uids = [line.strip().split(",")[0].strip()
            for line in uid_text.strip().splitlines() if line.strip()]
    if not uids:
        st.warning("UID를 한 개 이상 입력해 주세요.")
    else:
        results = []
        today_ymd = _today_str()
        progress = st.progress(0, text="조회 중...")
        for i, uid in enumerate(uids):
            res = lookup_member(uid, accounts)
            lg = ledger.get(uid, {})

            # 시트 J열의 최근 30일 거래량 (있으면), K열 갱신일시
            sheet_30d_raw = lg.get("최근 30일 거래량", "").strip()
            sheet_updated = lg.get("갱신일시", "").strip()
            has_valid_30d = False
            sheet_30d_val = None
            if sheet_30d_raw:
                try:
                    sheet_30d_val = float(sheet_30d_raw)
                    # 오늘 스캔한 값이면 판정에 사용
                    if sheet_updated.startswith(today_ymd):
                        has_valid_30d = True
                except ValueError:
                    pass

            row = {
                "트뷰 닉네임": lg.get("트뷰 닉네임") or "-",
                "UID": uid,
                "구분": lg.get("구분") or "-",
            }
            if res["found"]:
                # 판정: 시트의 오늘자 last 30d 값 우선. 없으면 "스캔 필요"
                if has_valid_30d:
                    passed = sheet_30d_val >= VOLUME_THRESHOLD
                    verdict = "달성" if passed else "미달"
                elif lg.get("구분") in ("트레이딩룸+지표", "트레이딩룸만"):
                    verdict = "스캔 필요"
                else:
                    verdict = "-"

                row.update({
                    "최근 30일 거래량": sheet_30d_val,
                    "갱신일시": sheet_updated or "-",
                    f"{PERIOD_LABEL} 거래량": res["month_volume"],
                    "기준 달성": verdict,
                    "전체기간 거래량": res["total_volume"],
                    "전체기간 커미션": res["total_commission"],
                    "누적 입금액": res["deposit"],
                })
            else:
                row.update({
                    "최근 30일 거래량": sheet_30d_val,
                    "갱신일시": sheet_updated or "-",
                    f"{PERIOD_LABEL} 거래량": None,
                    "기준 달성": res["error"],
                    "전체기간 거래량": None, "전체기간 커미션": None, "누적 입금액": None,
                })
            row.update({
                "트레이딩룸 입장일": lg.get("트레이딩룸 입장일") or "-",
                "입장시드": lg.get("입장시드") or "-",
                "지표 지급날짜": lg.get("지표 지급날짜") or "-",
                "실제 입장확인": lg.get("실제 입장확인") or "-",
                "제외일": lg.get("제외일") or "-",
                "메모": lg.get("메모") or "-",
            })
            results.append(row)
            progress.progress((i + 1) / len(uids), text=f"조회 중... ({i+1}/{len(uids)})")
        progress.empty()
        st.session_state["last_results"] = results

# ─── 조회 결과 ───
if st.session_state.get("last_results"):
    results = st.session_state["last_results"]
    df = pd.DataFrame(results)

    total = len(df)
    achieved = (df["기준 달성"] == "달성").sum()
    failed = (df["기준 달성"] == "미달").sum()
    need_scan = (df["기준 달성"] == "스캔 필요").sum()
    in_ledger = (df["구분"] != "-").sum()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("전체", f"{total}명")
    c2.metric("기준 달성", f"{achieved}명")
    c3.metric("기준 미달", f"{failed}명")
    c4.metric("스캔 필요", f"{need_scan}명")
    c5.metric("대장 매칭", f"{in_ledger}명")

    if need_scan > 0:
        st.info(f"⚠️ {need_scan}명은 오늘 스캔된 최근 30일 거래량이 없어 판정 보류. "
                f"상단 '📡 최근 30일 거래량 스캔'을 먼저 실행하세요.")

    if failed > 0:
        with st.expander(f"📋 미달자 UID 모아보기 ({failed}명)"):
            miss = df[df["기준 달성"] == "미달"][
                ["트뷰 닉네임", "UID", "최근 30일 거래량", "트레이딩룸 입장일"]
            ].copy()
            st.code("\n".join(miss["UID"].tolist()), language=None)
            miss["최근 30일 거래량"] = miss["최근 30일 거래량"].apply(
                lambda v: f"${v:,.0f}" if v is not None else "-"
            )
            st.dataframe(miss, use_container_width=True, hide_index=True)

    money_cols = [f"{PERIOD_LABEL} 거래량", "최근 30일 거래량", "전체기간 거래량", "전체기간 커미션", "누적 입금액"]
    df_show = df.copy()
    for col in money_cols:
        df_show[col] = df_show[col].apply(lambda v: f"${v:,.0f}" if v is not None else "-")

    st.subheader("결과")
    st.dataframe(df_show, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("결과 CSV 다운로드", data=csv,
                       file_name="volume_status.csv", mime="text/csv")

    # ─── 미달자 일괄 제외 처리 ───
    st.divider()
    st.subheader("🚫 미달자 일괄 제외 처리")

    excludable = [
        r for r in results
        if r["기준 달성"] == "미달"
        and r["구분"] in ("트레이딩룸+지표", "트레이딩룸만")
    ]

    if not excludable:
        st.info("이번 조회 결과 중 제외 처리 대상 미달자가 없어요. "
                "(트레이딩룸 관련 회원 & 기준 미달인 사람만 표시)")
    else:
        st.write(f"기준 미달인 트레이딩룸 회원 **{len(excludable)}명** 발견")
        preview_df = pd.DataFrame([
            {"UID": r["UID"],
             "닉네임": r["트뷰 닉네임"],
             "구분": r["구분"],
             "최근 30일 거래량": r["최근 30일 거래량"]}
            for r in excludable
        ])
        preview_df["최근 30일 거래량"] = preview_df["최근 30일 거래량"].apply(
            lambda v: f"${v:,.0f}" if v is not None else "-"
        )
        st.dataframe(preview_df, use_container_width=True, hide_index=True)

        selected_excl = st.multiselect(
            "제외 처리할 UID 선택 (기본값: 전체)",
            options=[r["UID"] for r in excludable],
            default=[r["UID"] for r in excludable],
            key="exclude_select",
        )

        st.caption(
            "💡 구분을 '제외'로 바꾸고 H열(제외일)에 오늘 날짜를 자동 기록합니다. "
            "D/E/F 열 옛날 값은 그대로 유지 (이탈 프로파일 분석용)."
        )

        if st.button(f"🚫 선택한 {len(selected_excl)}명 '제외' 처리",
                     type="primary", key="exclude_btn"):
            if not selected_excl:
                st.warning("UID를 한 개 이상 선택하세요.")
            else:
                try:
                    ws = get_worksheet()
                    today = _today_str()
                    edits = []
                    for uid in selected_excl:
                        lg = ledger.get(uid, {})
                        col_updates = [(COL_GUBUN, "제외")]
                        if not lg.get("제외일", "").strip():
                            col_updates.append((COL_EXCLUDED, today))
                        edits.append({"uid": uid, "col_updates": col_updates})

                    cells, unfound = apply_edits_batch(ws, edits)
                    load_ledger.clear()
                    st.success(f"✅ {len(selected_excl)}명 '제외' 처리 완료")
                    if unfound:
                        st.warning(f"시트에 없는 UID: {', '.join(unfound)}")
                    st.session_state.pop("last_results", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"처리 실패: {e}")

    # ─── 통합대장 편집 ───
    st.divider()
    st.subheader("✏️ 통합대장 편집")

    editable_uids = [r["UID"] for r in results if r["구분"] != "-"]

    if not editable_uids:
        st.info("이번 조회 결과 중 통합대장에 있는 UID가 없어요. "
                "신규 멤버는 위 '➕ 신규 멤버 추가하기 / 기존 멤버 업그레이드'에서 등록하세요.")
    else:
        selected_edit = st.multiselect(
            "편집할 UID 선택 (조회한 목록 중 대장에 있는 것만)",
            options=editable_uids, key="edit_select",
        )

        if selected_edit:
            edit_rows = []
            for uid in selected_edit:
                lg = ledger.get(uid, {})
                edit_rows.append({
                    "UID": uid,
                    "트뷰 닉네임": lg.get("트뷰 닉네임", ""),
                    "구분": lg.get("구분", ""),
                    "입장시드": lg.get("입장시드", ""),
                })
            edit_df_before = pd.DataFrame(edit_rows)

            sel_hash = hashlib.md5(
                "_".join(sorted(selected_edit)).encode()
            ).hexdigest()[:8]

            edited = st.data_editor(
                edit_df_before,
                column_config={
                    "UID": st.column_config.TextColumn(disabled=True),
                    "구분": st.column_config.SelectboxColumn(
                        options=GUBUN_OPTIONS, required=True
                    ),
                },
                hide_index=True,
                use_container_width=True,
                key=f"edit_table_{sel_hash}",
            )

            st.caption(
                "💡 구분을 바꾸면 관련 날짜(D/F/H)가 **비어있을 때만** 오늘 날짜로 자동 반영됩니다. "
                "이미 값이 있으면 그대로 유지."
            )

            if st.button("💾 변경사항 시트에 저장", type="primary"):
                today = _today_str()
                edits = []
                changed_uids = []

                for i in range(len(edited)):
                    uid = str(edited.iloc[i]["UID"])
                    lg = ledger.get(uid, {})
                    col_updates = []

                    orig_nick = str(edit_df_before.iloc[i]["트뷰 닉네임"]).strip()
                    new_nick = str(edited.iloc[i]["트뷰 닉네임"]).strip()
                    if new_nick != orig_nick:
                        col_updates.append((COL_NICK, new_nick))

                    orig_seed = str(edit_df_before.iloc[i]["입장시드"]).strip()
                    new_seed = str(edited.iloc[i]["입장시드"]).strip()
                    if new_seed != orig_seed:
                        col_updates.append((COL_SEED, new_seed))

                    orig_gubun = str(edit_df_before.iloc[i]["구분"]).strip()
                    new_gubun = str(edited.iloc[i]["구분"]).strip()
                    if new_gubun != orig_gubun:
                        col_updates.append((COL_GUBUN, new_gubun))
                        if "트레이딩룸" in new_gubun and not lg.get("트레이딩룸 입장일", "").strip():
                            col_updates.append((COL_ROOM_DATE, today))
                        if "지표" in new_gubun and not lg.get("지표 지급날짜", "").strip():
                            col_updates.append((COL_IND_DATE, today))
                        if new_gubun == "제외" and not lg.get("제외일", "").strip():
                            col_updates.append((COL_EXCLUDED, today))

                    if col_updates:
                        edits.append({"uid": uid, "col_updates": col_updates})
                        changed_uids.append(uid)

                if not edits:
                    st.info("변경사항이 없어요.")
                else:
                    try:
                        ws = get_worksheet()
                        cells_updated, unfound = apply_edits_batch(ws, edits)
                        load_ledger.clear()
                        st.success(
                            f"✅ {len(changed_uids)}명 정보 업데이트 완료 "
                            f"(총 {cells_updated}칸 변경)"
                        )
                        if unfound:
                            st.warning(f"시트에 없는 UID: {', '.join(set(unfound))}")
                        st.session_state.pop("last_results", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"저장 실패: {e}")

    # ─── 실제 입장확인 일괄 처리 ───
    st.divider()
    st.subheader("✅ 실제 입장확인 처리")
    unconfirmed = [
        r["UID"] for r in results
        if r["구분"] != "-" and r["실제 입장확인"] in ("-", "")
    ]
    if not unconfirmed:
        st.info("이번 조회 결과 중 통합대장에 있으면서 아직 확인 안 된 UID가 없어요.")
    else:
        st.write(f"통합대장에 있지만 아직 확인 안 된 UID **{len(unconfirmed)}개**")
        selected = st.multiselect("확인 처리할 UID 선택",
                                  options=unconfirmed, default=unconfirmed,
                                  key="confirm_select")
        if st.button(f"✅ 선택한 {len(selected)}개 UID에 '확인' 표시"):
            if not selected:
                st.warning("UID를 한 개 이상 선택하세요.")
            else:
                try:
                    ws = get_worksheet()
                    ok, fail = mark_confirmed_batch(ws, selected)
                    load_ledger.clear()
                    st.success(f"✅ {len(ok)}개 처리 완료"
                               + (f" / 실패 {len(fail)}개" if fail else ""))
                    if fail:
                        st.warning(f"실패한 UID (시트에 없음): {', '.join(fail)}")
                    st.session_state.pop("last_results", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"처리 실패: {e}")

st.caption("※ 현재 잔고는 OKX가 추천인에게 제공하지 않아 표시할 수 없어요. 대신 누적 입금액을 보여줍니다.")
