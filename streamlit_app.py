"""
PropTech AI 부동산 브리핑 — Streamlit Cloud 단일 파일 배포용
백엔드(FastAPI) 없이 Streamlit에서 직접 모든 로직 실행
"""

import streamlit as st
import os, json, requests, urllib.parse, concurrent.futures
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from openai import OpenAI

# ============================================================
# 0. API 키 로드 (Streamlit Cloud: secrets / 로컬: .env 폴백)
# ============================================================
def _get_secret(key: str) -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, "")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENAI_API_KEY = _get_secret("OPENAI_API_KEY")
MOLIT_API_KEY  = _get_secret("MOLIT_API_KEY")
def get_openai_client():
    key = _get_secret("OPENAI_API_KEY")
    if not key:
        st.error("OPENAI_API_KEY 없음 — Secrets 설정 필요")
        st.stop()
    return OpenAI(api_key=key)

def get_molit_key():
    return _get_secret("MOLIT_API_KEY")

# ============================================================
# 1. 유틸리티
# ============================================================

LAWD_CD_MAP = {
    # 서울
    "종로구":"11110","서울 중구":"11140","용산구":"11170",
    "성동구":"11200","광진구":"11215","동대문구":"11230",
    "중랑구":"11260","성북구":"11290","강북구":"11305",
    "도봉구":"11320","노원구":"11350","은평구":"11380",
    "서대문구":"11410","마포구":"11440","양천구":"11470",
    "강서구":"11500","구로구":"11530","금천구":"11545",
    "영등포구":"11560","동작구":"11590","관악구":"11620",
    "서초구":"11650","강남구":"11680","송파구":"11710","강동구":"11740",
    # 경기
    "수원시 장안구":"41111","수원시 권선구":"41113",
    "수원시 팔달구":"41115","수원시 영통구":"41117",
    "성남시 수정구":"41131","성남시 중원구":"41133","성남시 분당구":"41135",
    "의정부시":"41150","안양시 만안구":"41171","안양시 동안구":"41173",
    "부천시":"41190","광명시":"41210","평택시":"41220",
    "안산시 상록구":"41271","안산시 단원구":"41273",
    "고양시 덕양구":"41281","고양시 일산동구":"41285","고양시 일산서구":"41287",
    "과천시":"41290","구리시":"41310","남양주시":"41360","하남시":"41450",
    "용인시 처인구":"41461","용인시 기흥구":"41463","용인시 수지구":"41465",
    "파주시":"41480","화성시":"41590","광주시":"41610",
    "김포시":"41570","시흥시":"41390","군포시":"41410","의왕시":"41430",
    # 인천
    "인천 중구":"28110","인천 동구":"28140","미추홀구":"28177",
    "연수구":"28185","남동구":"28200","부평구":"28237",
    "계양구":"28245","인천 서구":"28260",
    # 부산
    "부산 중구":"26110","부산 서구":"26140","부산 동구":"26170",
    "영도구":"26200","부산진구":"26230","동래구":"26260",
    "부산 남구":"26290","부산 북구":"26320","해운대구":"26350",
    "사하구":"26380","금정구":"26410","부산 강서구":"26440",
    "연제구":"26470","수영구":"26500","사상구":"26530",
    # 대구
    "대구 중구":"27110","대구 동구":"27140","대구 서구":"27170",
    "대구 남구":"27200","대구 북구":"27230","수성구":"27260","달서구":"27290",
    # 대전
    "대전 동구":"30110","대전 중구":"30140","대전 서구":"30170",
    "유성구":"30200","대덕구":"30230",
    # 광주
    "광주 동구":"29110","광주 서구":"29140","광주 남구":"29155",
    "광주 북구":"29170","광산구":"29200",
    # 울산
    "울산 중구":"31110","울산 남구":"31140","울산 동구":"31170","울산 북구":"31200",
    # 세종
    "세종":"36110",
}

def extract_lawd_cd(address: str) -> str | None:
    if not address:
        return None
    norm = (address
            .replace("특별시","").replace("광역시","").replace("특별자치시","")
            .replace("경기도 ","").replace("전라남도 ","").replace("전라북도 ","")
            .replace("충청남도 ","").replace("충청북도 ","").replace("경상남도 ","")
            .replace("경상북도 ","").replace("강원도 ","").replace("강원특별자치도 ","")
            .replace("제주특별자치도 ",""))
    for key, code in LAWD_CD_MAP.items():
        if all(p in norm for p in key.split()):
            return code
    return None

def parse_price(s: str) -> int:
    if not s:
        return 0
    try:
        s = str(s).replace(" ", "").replace(",", "")
        if "억" in s:
            p = s.split("억")
            v = int(p[0]) * 10000
            if p[1]:
                v += int(p[1])
            return v
        return int(s)
    except:
        return 0

def fmt(v: int) -> str:
    if v <= 0:
        return "정보없음"
    eok, rem = divmod(v, 10000)
    if eok and rem:
        return f"{eok}억 {rem:,}만"
    return f"{eok}억" if eok else f"{rem:,}만"

def area_to_pyeong(area) -> str:
    try:
        m2 = float(str(area).replace("㎡","").strip())
        return f"{round(m2/3.3058)}평형({round(m2)}㎡)"
    except:
        return str(area)

def get_ym_list(months=36):
    now = datetime.now()
    return [(now - relativedelta(months=i)).strftime("%Y%m") for i in range(months)]

# ============================================================
# 2. 단지명 추론 (gpt-4o-mini) — 캐시로 재호출 방지
# ============================================================
@st.cache_data(show_spinner=False)
def resolve_complex_name(user_input: str) -> tuple[str, str]:
    resp = get_openai_client().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "당신은 한국 부동산 전문가입니다. 사용자 입력(단지명·주소·필지명·재개발구역명 등)에서 "
                "네이버 부동산 검색에 최적화된 키워드를 추론하세요.\n"
                "예: '은마'→'은마아파트', '반포 1구역'→'래미안원베일리', "
                "'압구정 현대'→'압구정현대아파트'\n"
                "반드시 아래 JSON 형식으로만 응답하세요:\n"
                '{"search_keyword":"...","reasoning":"한 줄 근거"}'
            )},
            {"role": "user", "content": user_input},
        ],
        temperature=0.1,
    )
    try:
        r = json.loads(resp.choices[0].message.content)
        return r.get("search_keyword", user_input), r.get("reasoning", "")
    except:
        return user_input, ""

# ============================================================
# 3. 네이버 단지 검색
# ============================================================
NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://new.land.naver.com/",
    "Accept": "application/json",
}

@st.cache_data(show_spinner=False, ttl=300)   # 5분 캐시
def search_naver_complex(keyword: str) -> dict | None:
    try:
        url = f"https://new.land.naver.com/api/search?keyword={urllib.parse.quote(keyword)}"
        r = requests.get(url, headers=NAVER_HEADERS, timeout=8)
        complexes = r.json().get("complexes", [])
        if not complexes:
            return None
        c = complexes[0]
        return {
            "complex_no":       str(c.get("complexNo", "")),
            "complex_name":     c.get("complexName", keyword),
            "address":          c.get("cortarAddress", ""),
            "total_households": c.get("totalHouseholdCount", ""),
            "completion_year":  c.get("completionYear", ""),
        }
    except:
        return None

# ============================================================
# 4. 네이버 매물 (중복제거 + 평형별 그룹화)
# ============================================================
@st.cache_data(show_spinner=False, ttl=180)   # 3분 캐시
def fetch_naver_listings(complex_no: str) -> dict:
    try:
        url = (f"https://new.land.naver.com/api/articles/complex/{complex_no}"
               f"?realEstateType=APT&tradeType=A1&sort=prc&pageSize=100")
        articles = requests.get(url, headers=NAVER_HEADERS, timeout=10).json().get("articleList", [])
        if not articles:
            return {"error": "매물 없음", "total_count": 0}

        # 중복제거: 동+층 동일 → 최저가 유지
        seen = {}
        for a in articles:
            dong  = a.get("buildingName", "")
            floor = a.get("floorInfo", "").split("/")[0].strip()
            key   = f"{dong}__{floor}"
            price = parse_price(a.get("dealOrWarrantPrc", ""))
            if key not in seen or seen[key]["_price"] > price:
                a["_price"] = price
                seen[key]   = a

        deduped = sorted(seen.values(), key=lambda x: x["_price"])

        def type_label(a):
            area_name = a.get("areaName", "")
            try:
                m2 = float(str(a.get("area1", "0")))
                return f"{round(m2/3.3058)}평({area_name})" if area_name else area_to_pyeong(m2)
            except:
                return area_name or "기타"

        by_type: dict[str, list] = {}
        all_prices = []
        for a in deduped:
            label = type_label(a)
            price = a["_price"]
            if price > 0:
                all_prices.append(price)
            listing = {
                "price":     a.get("dealOrWarrantPrc", ""),
                "price_val": price,
                "dong":      a.get("buildingName", ""),
                "floor":     a.get("floorInfo", ""),
                "area_m2":   a.get("area1", ""),
                "area_name": a.get("areaName", ""),
                "direction": a.get("direction", ""),
                "feature":   a.get("articleFeatureDesc", ""),
            }
            by_type.setdefault(label, []).append(listing)

        best5 = [
            {
                "rank": i+1,
                "price": a.get("dealOrWarrantPrc",""),
                "type":  type_label(a),
                "dong":  a.get("buildingName",""),
                "floor": a.get("floorInfo",""),
                "direction": a.get("direction",""),
                "feature":   a.get("articleFeatureDesc",""),
            }
            for i, a in enumerate(deduped[:5])
        ]

        type_summary = {}
        for label, listings in by_type.items():
            prices = [l["price_val"] for l in listings if l["price_val"] > 0]
            type_summary[label] = {
                "count":          len(listings),
                "min_price":      fmt(min(prices)) if prices else "정보없음",
                "max_price":      fmt(max(prices)) if prices else "정보없음",
                "min_price_val":  min(prices) if prices else 0,
                "max_price_val":  max(prices) if prices else 0,
                "best5_listings": listings[:5],
            }

        return {
            "total_count": len(deduped),
            "min_price":   fmt(min(all_prices)) if all_prices else "정보없음",
            "max_price":   fmt(max(all_prices)) if all_prices else "정보없음",
            "best5":       best5,
            "by_type":     type_summary,
        }
    except Exception as e:
        return {"error": str(e), "total_count": 0}

# ============================================================
# 5. 국토부 실거래가 (36개월 병렬)
# ============================================================
MOLIT_URL = (
    "http://openapi.molit.go.kr:8081/OpenAPI_ToolInstallPackage"
    "/service/rest/RTMSOBJSvc/getRTMSDataSvcAptTrade"
)

def _fetch_month(lawd_cd, ym, complex_name):
    try:
        params = {"serviceKey": get_molit_key(), "LAWD_CD": lawd_cd,
                  "DEAL_YMD": ym, "numOfRows": "100", "pageNo": "1"}
        res = requests.get(MOLIT_URL, params=params, timeout=6)
        if res.status_code != 200:
            return []
        root = ET.fromstring(res.text)
        result = []
        ml = min(3, len(complex_name))
        for item in root.findall(".//item"):
            nm = item.findtext("단지명", "").strip()
            if complex_name[:ml] not in nm and nm[:ml] not in complex_name:
                continue
            try:
                amount = int(item.findtext("거래금액","0").replace(",","").strip())
            except:
                amount = 0
            area = item.findtext("전용면적","0").strip()
            year  = item.findtext("년","").strip()
            month = item.findtext("월","").strip().zfill(2)
            if amount > 0:
                result.append({
                    "ym": f"{year}.{month}", "price_val": amount,
                    "price": fmt(amount), "area": area,
                    "pyeong": area_to_pyeong(area),
                    "floor": item.findtext("층","").strip(),
                })
        return result
    except:
        return []

@st.cache_data(show_spinner=False, ttl=3600)   # 1시간 캐시
def fetch_molit_transactions(lawd_cd: str, complex_name: str) -> dict:
    if not lawd_cd:
        return {"error": "지역코드 없음"}
    all_txns = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_fetch_month, lawd_cd, ym, complex_name) for ym in get_ym_list(36)]
        for f in concurrent.futures.as_completed(futs):
            all_txns.extend(f.result())
    if not all_txns:
        return {"error": "거래 데이터 없음"}
    all_txns.sort(key=lambda x: x["ym"], reverse=True)
    by_pyeong: dict[str, list] = {}
    for t in all_txns:
        by_pyeong.setdefault(t["pyeong"], []).append(t)
    summary = {}
    for pyeong, txns in by_pyeong.items():
        prices = [t["price_val"] for t in txns]
        monthly: dict[str, list] = {}
        for t in txns:
            monthly.setdefault(t["ym"], []).append(t["price_val"])
        trend = [{"ym": ym, "avg_val": sum(v)//len(v), "avg": fmt(sum(v)//len(v)), "count": len(v)}
                 for ym in sorted(monthly)]
        summary[pyeong] = {
            "count": len(txns), "avg": fmt(sum(prices)//len(prices)),
            "min": fmt(min(prices)), "max": fmt(max(prices)),
            "trend": trend, "recent10": txns[:10],
        }
    all_prices = [t["price_val"] for t in all_txns]
    return {
        "total_count": len(all_txns),
        "overall_avg": fmt(sum(all_prices)//len(all_prices)),
        "by_pyeong":   summary,
    }

# ============================================================
# 6. gpt-4o-mini 분석
# ============================================================
@st.cache_data(show_spinner=False)
def analyze_with_gpt(complex_name, address, households, year,
                     listing_summary, txn_summary, original_input) -> dict:
    prompt = f"""
[단지] {complex_name} / {address} / {households}세대 / {year}년

[현재 매물]
{listing_summary}

[3년 실거래가]
{txn_summary}

아래 JSON만 반환:
{{
  "target_summary":    "단지 한 줄 요약(입지+연식+규모)",
  "market_assessment": "호가 vs 실거래가 비교 분석(3~4문장, 갭 언급)",
  "by_type_analysis":  "평형별 투자 가치 비교",
  "investment_outlook":"매수 판단 및 전망(3문장)",
  "caution_points":    "리스크 1~2가지"
}}
"""
    resp = get_openai_client().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "10년 경력 부동산 애널리스트. 데이터 기반, 솔직하게. 반드시 JSON으로만 응답."},
            {"role": "user",   "content": f"'{original_input}' 분석:\n{prompt}"},
        ],
        temperature=0.2,
    )
    try:
        content = resp.choices[0].message.content
        start = content.find("{")
        end = content.rfind("}") + 1
        return json.loads(content[start:end])
    except:
        return {"error": "분석 실패"}


# ============================================================
# 7. Streamlit UI
# ============================================================
st.set_page_config(
    page_title="PropTech AI 부동산 애널리스트",
    page_icon="🏘️",
    layout="wide",
)

st.markdown("""
<style>
.listing-row {
    border-left: 3px solid #3d9970;
    padding: 8px 12px; margin: 5px 0;
    background:#f9fdf9; border-radius: 0 6px 6px 0;
}
.txn-row {
    border-left: 3px solid #0074d9;
    padding: 6px 10px; margin: 4px 0;
    font-size:.88em; background:#f5f8fd;
    border-radius: 0 6px 6px 0;
}
</style>
""", unsafe_allow_html=True)

st.title("🏘️ PropTech AI 부동산 브리핑")
st.markdown("단지명, 주소, 필지명, 재개발 구역명 등 **어떤 형태로든** 입력하세요.")

col_in, col_btn = st.columns([5, 1])
with col_in:
    q = st.text_input("검색", label_visibility="collapsed",
                      placeholder="예: 은마아파트 / 반포 1구역 / 압구정 현대 / 헬리오시티",
                      key="q")
with col_btn:
    go = st.button("🔍 분석", type="primary", use_container_width=True)

st.divider()

if (go or q) and q:
    if st.session_state.get("_last") == q and not go:
        st.stop()
    st.session_state["_last"] = q

    # ── 단계별 진행 표시 ──
    progress_placeholder = st.empty()

    with progress_placeholder.container():
        with st.status("AI 분석 중...", expanded=True) as status_box:
            st.write("🧠 단지명 추론 중 (gpt-4o-mini)...")
            keyword, reasoning = resolve_complex_name(q)

            st.write(f"🔍 '{keyword}' 네이버 검색 중...")
            info = search_naver_complex(keyword) or search_naver_complex(q)
            if not info:
                status_box.update(label="단지를 찾을 수 없습니다", state="error")
                st.error(f"'{q}'에 해당하는 단지를 찾을 수 없습니다.")
                st.stop()

            st.write(f"📡 '{info['complex_name']}' 매물 수집 중 (중복제거)...")
            listings = fetch_naver_listings(info["complex_no"])

            lawd_cd = extract_lawd_cd(info["address"])
            st.write(f"📋 국토부 실거래가 36개월 조회 중 (LAWD_CD: {lawd_cd or '미지원 지역'})...")
            transactions = (fetch_molit_transactions(lawd_cd, info["complex_name"])
                            if lawd_cd else {"error": "지역코드 없음"})

            st.write("🤖 gpt-4o-mini 투자 분석 중...")
            # 분석용 요약 문자열 생성
            l_lines = [
                f"총 매물: {listings.get('total_count',0)}건 (중복제거)",
                f"호가: {listings.get('min_price','?')} ~ {listings.get('max_price','?')}",
            ]
            for tn, td in listings.get("by_type", {}).items():
                l_lines.append(f"  [{tn}] {td['count']}건 | {td['min_price']}~{td['max_price']}")

            t_lines = []
            if "error" not in transactions:
                t_lines.append(f"총 {transactions.get('total_count',0)}건, 평균 {transactions.get('overall_avg','')}")
                for pyeong, d in transactions.get("by_pyeong", {}).items():
                    t_lines.append(f"  [{pyeong}] {d['count']}건 | 평균{d['avg']} | {d['min']}~{d['max']}")
            else:
                t_lines.append("실거래가 없음 — 학습 데이터 보완")

            analysis = analyze_with_gpt(
                info["complex_name"], info["address"],
                info["total_households"], info["completion_year"],
                "\n".join(l_lines), "\n".join(t_lines), q,
            )
            status_box.update(label="분석 완료! ✅", state="complete", expanded=False)

    progress_placeholder.empty()

    # ── 단지 헤더 ──
    naver_link = f"https://new.land.naver.com/complexes/{info['complex_no']}"
    h1, h2, h3 = st.columns([4, 1, 1])
    with h1:
        st.subheader(f"🏢 {info['complex_name']}")
        st.caption(f"📍 {info['address']}")
        if reasoning:
            st.caption(f"💡 AI 추론: {reasoning}")
    with h2:
        st.metric("세대수", f"{info['total_households']}세대" if info['total_households'] else "–")
    with h3:
        st.metric("입주연도", info['completion_year'] or "–")

    st.link_button("👉 네이버 부동산에서 전체 매물 보기", naver_link, type="secondary")
    st.divider()

    # ── 탭 ──
    tab1, tab2, tab3 = st.tabs(["🛒 현재 매물 (호가)", "📊 실거래가 (3년)", "🤖 AI 투자 분석"])

    # ── 탭1: 현재 매물 ──
    with tab1:
        if listings.get("error") and listings.get("total_count", 0) == 0:
            st.warning("네이버 매물 데이터를 불러올 수 없습니다.")
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("총 매물 (중복제거)", f"{listings.get('total_count',0)}건")
            m2.metric("최저 호가", listings.get("min_price","–"))
            m3.metric("최고 호가", listings.get("max_price","–"))
            st.markdown("---")

            st.markdown("#### 🏆 전체 Best 5 (최저가 순)")
            for item in listings.get("best5", []):
                feat = item.get("feature","")
                st.markdown(
                    f'<div class="listing-row"><strong>#{item["rank"]} 매매 {item["price"]}</strong>'
                    f' &nbsp;|&nbsp; {item.get("type","")} &nbsp;|&nbsp; '
                    f'{item["dong"]} {item["floor"]} &nbsp;|&nbsp; {item["direction"]}'
                    f' &nbsp;|&nbsp; <span style="color:#777">{(feat[:30]+"…") if len(feat)>32 else feat}</span></div>',
                    unsafe_allow_html=True,
                )

            st.markdown("---")
            st.markdown("#### 📐 평형별 상세")
            sorted_types = sorted(
                listings.get("by_type", {}).keys(),
                key=lambda t: int(t.split("평")[0]) if t[0].isdigit() else 0,
            )
            for tname in sorted_types:
                td = listings["by_type"][tname]
                with st.expander(f"{tname}  ·  {td['count']}건  ·  {td['min_price']} ~ {td['max_price']}"):
                    for i, l in enumerate(td.get("best5_listings", [])):
                        c1, c2, c3, c4 = st.columns([2, 1.2, 1, 2])
                        c1.markdown(f"**매매 {l['price']}**")
                        c2.caption(f"📍 {l['dong']} {l['floor']}")
                        c3.caption(f"🧭 {l['direction']}")
                        feat = l.get("feature","")
                        c4.caption((feat[:30]+"…") if len(feat)>32 else feat)
                        if i < len(td["best5_listings"])-1:
                            st.divider()

    # ── 탭2: 실거래가 ──
    with tab2:
        if transactions.get("error"):
            st.warning(f"실거래가 데이터 없음 — {transactions['error']}")
        else:
            t1, t2 = st.columns(2)
            t1.metric("3년 총 거래", f"{transactions['total_count']:,}건")
            t2.metric("전체 평균가", transactions["overall_avg"])

            by_pyeong = transactions.get("by_pyeong", {})
            rows = []
            for pyeong, pd_ in by_pyeong.items():
                for pt in pd_.get("trend", []):
                    rows.append({"날짜": pt["ym"], "평형": pyeong, "평균가(만원)": pt["avg_val"]})
            if rows:
                df = pd.DataFrame(rows).pivot_table(
                    index="날짜", columns="평형", values="평균가(만원)", aggfunc="mean"
                ).sort_index()
                st.markdown("#### 📈 평형별 실거래가 트렌드")
                st.line_chart(df, height=300)

            st.markdown("---")
            sorted_pyeong = sorted(
                by_pyeong.keys(),
                key=lambda t: int(t.split("평")[0]) if t[0].isdigit() else 0,
            )
            for pyeong in sorted_pyeong:
                pd_ = by_pyeong[pyeong]
                with st.expander(
                    f"{pyeong}  ·  {pd_['count']}건  ·  평균 {pd_['avg']}  ·  {pd_['min']}~{pd_['max']}"
                ):
                    for t in pd_.get("recent10", []):
                        st.markdown(
                            f'<div class="txn-row">📅 {t["ym"]} &nbsp;|&nbsp; '
                            f'💰 <strong>{t["price"]}</strong> &nbsp;|&nbsp; '
                            f'{t["area"]}㎡ &nbsp;|&nbsp; {t["floor"]}층</div>',
                            unsafe_allow_html=True,
                        )

    # ── 탭3: AI 분석 ──
    with tab3:
        if analysis.get("error"):
            st.error("AI 분석 생성 실패")
        else:
            st.info(f"🎯 **단지 요약**\n\n{analysis.get('target_summary','')}")
            col_l, col_r = st.columns(2)
            with col_l:
                st.warning(f"📊 **시세 분석**\n\n{analysis.get('market_assessment','')}")
                st.success(f"💰 **투자 전망**\n\n{analysis.get('investment_outlook','')}")
            with col_r:
                st.info(f"📐 **평형별 가치**\n\n{analysis.get('by_type_analysis','')}")
                st.error(f"⚠️ **리스크**\n\n{analysis.get('caution_points','')}")
        st.caption("💡 AI 참고용 분석입니다. 실제 투자 시 현장 방문·전문가 상담을 권장합니다.")
