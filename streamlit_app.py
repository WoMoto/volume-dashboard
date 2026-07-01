"""
streamlit_app.py - 거래량 대시보드 v5
v4에서 추가:
- 비밀번호 잠금 (DASHBOARD_PASSWORD)
- 통합 관리대장 쓰기 (서비스 계정 인증)
  - 신규 멤버 추가
  - 실제 입장확인 일괄 처리
- 시트 읽기를 gspread로 통일 (시트 비공개 가능)
"""

import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone

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

# 시트 컬럼 (1-based)
COL_UID = 1
COL_NICK = 2
COL_GUBUN = 3
COL_ROOM_DATE = 4
COL_SEED = 5
COL_IND_DATE = 6
COL_CONFIRMED = 7
COL_MEMO = 8

LEDGER_COLS = [
    "트뷰 닉네임", "구분", "트레이딩룸 입장일", "입장시드",
    "지표 지급날짜", "실제 입장확인", "메모",
]
GUBUN_OPTIONS = ["트레이딩룸+지표", "트레이딩룸만", "지표만"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


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
            "affiliate_code": "41888826",
        })
    except Exception:
        pass
    try:
        b = st.secrets["okx_b"]
        accounts.append({
            "name": "계정B", "api_key": b["api_key"],
            "secret_key": b["secret_key"], "passphrase": b["passphrase"],
            "affiliate_code": "BULDAN",
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
            if data.get("affiliateCode") == account["affiliate_code"]:
                return {
                    "found": True, "error": None,
                    "month_volume": _safe_float(data.get("volMonth")),
                    "total_volume": _safe_float(data.get("totalTradingVolume")),
                    "total_commission": _safe_float(data.get("totalCommission")),
                    "deposit": _safe_float(data.get("depAmt")),
                    "account": account["name"],
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
        records = ws.get_all_records()
        ledger = {}
        for r in records:
            uid = str(r.get("UID", "")).strip()
            if uid:
                ledger[uid] = {c: str(r.get(c, "")).strip() for c in LEDGER_COLS}
        return ledger, None
    except Exception as e:
        return {}, f"통합대장 불러오기 실패: {e}"


def append_member(ws, uid, nickname, gubun, room_date, seed, ind_date, memo):
    new_row = [uid, nickname, gubun, room_date, seed, ind_date, "", memo]
    ws.append_row(new_row, value_input_option="USER_ENTERED")


def mark_confirmed_batch(ws, uids):
    """선택된 UID들에 일괄로 '확인' 표시. (한 번의 API 호출)"""
    col_values = ws.col_values(COL_UID)
    uid_to_row = {
        str(v).strip(): i for i, v in enumerate(col_values, start=1)
        if str(v).strip()
    }
    success, fail, updates = [], [], []
    for uid in uids:
        row = uid_to_row.get(str(uid))
        if row is None:
            fail.append(uid)
            continue
        cell_addr = gspread.utils.rowcol_to_a1(row, COL_CONFIRMED)
        updates.append({"range": cell_addr, "values": [["확인"]]})
        success.append(uid)
    if updates:
        ws.batch_update(updates)
    return success, fail


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

# ─── 신규 멤버 추가 ───
with st.expander("➕ 신규 멤버 추가하기"):
    with st.form("add_member_form", clear_on_submit=True):
        st.caption("새 회원을 통합 관리대장에 등록합니다. *표시는 필수.")
        nm_uid = st.text_input("UID *")
        nm_nick = st.text_input("트뷰 닉네임")
        nm_gubun = st.selectbox("구분 *", GUBUN_OPTIONS)
        c1, c2 = st.columns(2)
        with c1:
            nm_room_date = st.text_input("트레이딩룸 입장일 (예: 2026. 6. 25)")
            nm_seed = st.text_input("입장시드")
        with c2:
            nm_ind_date = st.text_input("지표 지급날짜 (예: 2026. 6. 25)")
            nm_memo = st.text_input("메모")
        submitted = st.form_submit_button("✅ 통합대장에 추가", type="primary")

    if submitted:
        uid_clean = nm_uid.strip()
        if not uid_clean:
            st.error("UID를 입력하세요.")
        elif not uid_clean.isdigit():
            st.error("UID는 숫자만 가능해요.")
        elif not (15 <= len(uid_clean) <= 18):
            st.error(f"UID는 15~18자리여야 해요. (현재 {len(uid_clean)}자리)")
        elif uid_clean in ledger:
            st.error(f"이미 통합대장에 있는 UID예요. (구분: {ledger[uid_clean].get('구분')})")
        else:
            try:
                ws = get_worksheet()
                append_member(
                    ws, uid_clean, nm_nick.strip(), nm_gubun,
                    nm_room_date.strip(), nm_seed.strip(),
                    nm_ind_date.strip(), nm_memo.strip(),
                )
                load_ledger.clear()
                st.success(f"✅ {uid_clean} 추가 완료! (구분: {nm_gubun})")
            except Exception as e:
                st.error(f"추가 실패: {e}")

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
        progress = st.progress(0, text="조회 중...")
        for i, uid in enumerate(uids):
            res = lookup_member(uid, accounts)
            lg = ledger.get(uid, {})
            row = {
                "트뷰 닉네임": lg.get("트뷰 닉네임") or "-",
                "UID": uid,
                "구분": lg.get("구분") or "-",
            }
            if res["found"]:
                passed = res["month_volume"] >= VOLUME_THRESHOLD
                row.update({
                    f"{PERIOD_LABEL} 거래량": res["month_volume"],
                    "기준 달성": "달성" if passed else "미달",
                    "전체기간 거래량": res["total_volume"],
                    "전체기간 커미션": res["total_commission"],
                    "누적 입금액": res["deposit"],
                })
            else:
                row.update({
                    f"{PERIOD_LABEL} 거래량": None, "기준 달성": res["error"],
                    "전체기간 거래량": None, "전체기간 커미션": None, "누적 입금액": None,
                })
            row.update({
                "트레이딩룸 입장일": lg.get("트레이딩룸 입장일") or "-",
                "입장시드": lg.get("입장시드") or "-",
                "지표 지급날짜": lg.get("지표 지급날짜") or "-",
                "실제 입장확인": lg.get("실제 입장확인") or "-",
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
    in_ledger = (df["구분"] != "-").sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("전체", f"{total}명")
    c2.metric("기준 달성", f"{achieved}명")
    c3.metric("기준 미달", f"{failed}명")
    c4.metric("대장 매칭", f"{in_ledger}명")

    if failed > 0:
        with st.expander(f"📋 미달자 UID 모아보기 ({failed}명)"):
            miss = df[df["기준 달성"] == "미달"][
                ["트뷰 닉네임", "UID", f"{PERIOD_LABEL} 거래량", "트레이딩룸 입장일"]
            ].copy()
            st.code("\n".join(miss["UID"].tolist()), language=None)
            miss[f"{PERIOD_LABEL} 거래량"] = miss[f"{PERIOD_LABEL} 거래량"].apply(
                lambda v: f"${v:,.0f}" if v is not None else "-"
            )
            st.dataframe(miss, use_container_width=True, hide_index=True)

    money_cols = [f"{PERIOD_LABEL} 거래량", "전체기간 거래량", "전체기간 커미션", "누적 입금액"]
    df_show = df.copy()
    for col in money_cols:
        df_show[col] = df_show[col].apply(lambda v: f"${v:,.0f}" if v is not None else "-")

    st.subheader("결과")
    st.dataframe(df_show, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("결과 CSV 다운로드", data=csv,
                       file_name="volume_status.csv", mime="text/csv")

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
