"""
streamlit_app.py - 거래량 대시보드 v4
v3에서 추가: 구글시트 '통합 관리대장' 탭에서 트뷰 닉네임/구분/입장일/지표지급일/메모 등
            정보를 UID 기준으로 자동 매칭해서 함께 표시.
"""

import hmac
import hashlib
import base64
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import pandas as pd
import streamlit as st


VOLUME_THRESHOLD = 50_000
PERIOD_LABEL = "이번 달"
OKX_API_BASE = "https://www.okx.com"

# 통합 관리대장 (구글시트 공개 CSV)
SHEET_ID = "1kahCWtaZ35pbCa7XMBcrPNEXsGTQ28XTCTYA8qmL-uY"
SHEET_TAB = "통합 관리대장"

LEDGER_COLS = [
    "트뷰 닉네임", "구분", "트레이딩룸 입장일", "입장시드",
    "지표 지급날짜", "실제 입장확인", "메모",
]


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


@st.cache_data(ttl=300, show_spinner=False)
def load_ledger():
    """통합 관리대장을 UID 기준 dict로 읽어옴. (5분 캐시)"""
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={quote(SHEET_TAB)}"
    )
    try:
        df = pd.read_csv(url, dtype=str).fillna("")
        df.columns = [c.strip() for c in df.columns]
        if "UID" not in df.columns:
            return {}, f"시트에 UID 칸이 없어요. (찾은 칸: {list(df.columns)})"
        ledger = {}
        for _, row in df.iterrows():
            uid = str(row["UID"]).strip()
            if uid:
                ledger[uid] = {c: str(row.get(c, "")).strip() for c in LEDGER_COLS}
        return ledger, None
    except Exception as e:
        return {}, f"통합대장 불러오기 실패: {e}"


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
                    "found": True,
                    "error": None,
                    "month_volume": _safe_float(data.get("volMonth")),
                    "total_volume": _safe_float(data.get("totalTradingVolume")),
                    "total_commission": _safe_float(data.get("totalCommission")),
                    "deposit": _safe_float(data.get("depAmt")),
                    "account": account["name"],
                }

    return {"found": False, "error": "가입자 아님"}


# ─────────────────────────────────────────────
st.set_page_config(page_title="거래량 관리", page_icon="📊", layout="wide")
st.title("📊 트레이딩룸 거래량 현황")
st.caption(f"기준: {PERIOD_LABEL} 거래량 ${VOLUME_THRESHOLD:,} 이상")

accounts = load_accounts()
if not accounts:
    st.error(
        "OKX 계정 정보가 아직 설정되지 않았어요.\n\n"
        "Streamlit 사이트의 Settings -> Secrets 에 키를 넣어주세요."
    )
    st.stop()

# 통합 관리대장 로드
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

st.subheader("UID 입력")
st.write("확인할 UID를 한 줄에 하나씩 붙여넣으세요. 닉네임·입장일·메모는 통합대장에서 자동으로 불러옵니다.")

uid_text = st.text_area(
    "UID 목록", value="", height=200,
    placeholder="123456789012345\n234567890123456",
)

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
                    f"{PERIOD_LABEL} 거래량": None,
                    "기준 달성": res["error"],
                    "전체기간 거래량": None,
                    "전체기간 커미션": None,
                    "누적 입금액": None,
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

        # 미달자 UID 모아보기
        if failed > 0:
            miss = df[df["기준 달성"] == "미달"][
                ["트뷰 닉네임", "UID", f"{PERIOD_LABEL} 거래량", "트레이딩룸 입장일"]
            ].copy()
            miss[f"{PERIOD_LABEL} 거래량"] = miss[f"{PERIOD_LABEL} 거래량"].apply(
                lambda v: f"${v:,.0f}" if v is not None else "-"
            )
            with st.expander(f"📋 미달자 UID 모아보기 ({failed}명)"):
                st.code("\n".join(df[df["기준 달성"] == "미달"]["UID"].tolist()), language=None)
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

st.caption("※ 현재 잔고는 OKX가 추천인에게 제공하지 않아 표시할 수 없어요. 대신 누적 입금액을 보여줍니다.")
