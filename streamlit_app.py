"""
PropTech AI 부동산 브리핑 v3
- FastAPI 없이 Streamlit 단독 실행
- Streamlit Cloud 배포용 (st.secrets로 API 키 관리)
"""

import streamlit as st
import os, json, requests, urllib.parse, concurrent.futures
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from openai import OpenAI

# ============================================================
# 0. API 키 (Streamlit Cloud secrets 우선, .env 폴백)
# ============================================================
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def _secret(key: str) -> str:
    try:
        v = st.secrets[key]
        return v if v else os.getenv(key, "")
    except Exception:
        return os.getenv(key, "")

def _openai() -> OpenAI:
    key = _secret("OPENAI_API_KEY")
    if not key:
        st.error("❌ OPENAI_API_KEY 없음 — Streamlit Cloud > Manage app > Settings > Secrets 에서 추가하세요.")
        st.stop()
    return OpenAI(api_key=key)

def _molit_key() -> str:
    return _secret("MOLIT_API_KEY")

# ============================================================
# 1. 유틸
# ============================================================
LAWD_CD_MAP = {
    "종로구":"11110","서울 중구":"11140","용산구":"11170",
    "성동구":"11200","광진구":"11215","동대문구":"11230",
    "중랑구":"11260","성북구":"11290","강북구":"11305",
    "도봉구":"11320","노원구":"11350","은평구":"11380",
    "서대문구":"11410","마포구":"11440","양천구":"11470",
    "강서구":"11500","구로구":"11530","금천구":"11545",
    "영등포구":"11560","동작구":"11590","관악구":"11620",
    "서초구":"11650","강남구":"11680","송파구":"11710","강동구":"11740",
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
    "인천 중구":"28110","인천 동구":"28140","미추홀구":"28177",
    "연수구":"28185","남동구":"28200","부평구":"28237",
    "계양구":"28245","인천 서구":"28260",
    "부산 중구":"26110","부산 서구":"26140","부산 동구":"26170",
    "영도구":"26200","부산진구":"26230","동래구":"26260",
    "부산 남구":"26290","부산 북구":"26320","해운대구":"26350",
    "사하구":"26380","금정구":"26410","부산 강서구":"26440",
    "연제구":"26470","수영구":"26500","사상구":"26530",
    "대구 중구":"27110","대구 동구":"27140","대구 서구":"27170",
    "대구 남구":"27200","대구 북구":"27230","수성구":"27260","달서구":"27290",
    "대전 동구":"30110","대전 중구":"30140","대전 서구":"30170",
    "유성구":"30200","대덕구":"30230",
    "광주 동구":"29110","광주 서구":"29140","광주 남구":"29155",
    "광주 북구":"29170","광산구":"29200",
    "울산 중구":"31110","울산 남구":"31140","울산 동구":"31170","울산 북구":"31200",
    "세종":"36110",
}

def _lawd(address: str):
    if not address: return None
    n = (address
         .replace("특별시","").replace("광역시","").replace("특별자치시","")
         .replace("경기도 ","").replace("전라남도 ","").replace("전라북도 ","")
         .replace("충청남도 ","").replace("충청북도 ","").replace("경상남도 ","")
         .replace("경상북도 ","").replace("강원도 ","").replace("강원특별자치도 ","")
         .replace("제주특별자치도 ",""))
    for k, v in LAWD_CD_MAP.items():
        if all(p in n for p in k.split()): return v
    return None

def _parse(s) -> int:
    try:
        s = str(s).replace(" ","").replace(",","")
        if "억" in s:
            p = s.split("억"); v = int(p[0])*10000
            if p[1]: v += int(p[1])
            return v
        return int(s)
    except: return 0

def _fmt(v: int) -> str:
    if v <= 0: return "정보없음"
    e, r = divmod(v, 10000)
    if e and r: return f"{e}억 {r:,}만"
    return f"{e}억" if e else f"{r:,}만"

def _pyeong(area) -> str:
    try:
        m = float(str(area).replace("㎡","").strip())
        return f"{round(m/3.3058)}평형({round(m)}㎡)"
    except: return str(area)

def _yms(n=36):
    now = datetime.now()
    return [(now - relativedelta(months=i)).strftime("%Y%m") for i in range(n)]

def _parse_json(text: str) -> dict:
    """LLM 응답에서 JSON 부분만 추출해 파싱"""
    try:
        s = text.find("{"); e = text.rfind("}")+1
        return json.loads(text[s:e])
    except: return {}

# ============================================================
# 2. GPT: 단지명 추론
# ============================================================
@st.cache_data(show_spinner=False)
def resolve_name(user_input: str) -> tuple:
    try:
        resp = _openai().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content":
                    "한국 부동산 전문가. 사용자 입력에서 네이버 부동산 검색 최적 키워드 추론.\n"
                    "예: '은마'→'은마아파트', '반포 1구역'→'래미안원베일리'\n"
                    "반드시 JSON으로만 응답: {\"search_keyword\":\"...\",\"reasoning\":\"...\"}"},
                {"role": "user", "content": user_input},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        r = _parse_json(resp.choices[0].message.content)
        return r.get("search_keyword", user_input), r.get("reasoning", "")
    except Exception as e:
        return user_input, f"추론 오류: {e}"

# ============================================================
# 3. 네이버 단지 검색 (봇 차단 우회 헤더 강화)
# ============================================================
_NAV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://new.land.naver.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

@st.cache_data(show_spinner=False, ttl=300)
def search_complex(keyword: str):
    for attempt in [keyword, keyword.replace(" ",""), keyword.split()[0] if " " in keyword else keyword]:
        try:
            url = f"https://new.land.naver.com/api/search?keyword={urllib.parse.quote(attempt)}"
            r = requests.get(url, headers=_NAV_HEADERS, timeout=10)
            data = r.json()
            cx = data.get("complexes") or data.get("result", {}).get("complexes", [])
            if cx:
                c = cx[0]
                return {
                    "complex_no":       str(c.get("complexNo","")),
                    "complex_name":     c.get("complexName", keyword),
                    "address":          c.get("cortarAddress",""),
                    "total_households": c.get("totalHouseholdCount",""),
                    "completion_year":  c.get("completionYear",""),
                }
        except: pass
    return None

# ============================================================
# 4. 네이버 매물 (중복제거 + 평형별)
# ============================================================
@st.cache_data(show_spinner=False, ttl=180)
def get_listings(complex_no: str) -> dict:
    try:
        url = (f"https://new.land.naver.com/api/articles/complex/{complex_no}"
               f"?realEstateType=APT&tradeType=A1&sort=prc&pageSize=100")
        arts = requests.get(url, headers=_NAV_HEADERS, timeout=10).json().get("articleList", [])
        if not arts: return {"error":"매물없음","total_count":0}

        seen = {}
        for a in arts:
            k = f"{a.get('buildingName','')}_{a.get('floorInfo','').split('/')[0].strip()}"
            p = _parse(a.get("dealOrWarrantPrc",""))
            if k not in seen or seen[k]["_p"] > p:
                a["_p"] = p; seen[k] = a

        deduped = sorted(seen.values(), key=lambda x: x["_p"])

        def tlabel(a):
            nm = a.get("areaName","")
            try:
                m2 = float(str(a.get("area1","0")))
                return f"{round(m2/3.3058)}평({nm})" if nm else _pyeong(m2)
            except: return nm or "기타"

        by_type, all_p = {}, []
        for a in deduped:
            lb = tlabel(a); p = a["_p"]
            if p > 0: all_p.append(p)
            lst = {"price": a.get("dealOrWarrantPrc",""), "price_val": p,
                   "dong": a.get("buildingName",""), "floor": a.get("floorInfo",""),
                   "area_m2": a.get("area1",""), "area_name": a.get("areaName",""),
                   "direction": a.get("direction",""), "feature": a.get("articleFeatureDesc","")}
            by_type.setdefault(lb, []).append(lst)

        best5 = [{"rank":i+1,"price":a.get("dealOrWarrantPrc",""),"type":tlabel(a),
                  "dong":a.get("buildingName",""),"floor":a.get("floorInfo",""),
                  "direction":a.get("direction",""),"feature":a.get("articleFeatureDesc","")}
                 for i,a in enumerate(deduped[:5])]

        ts = {}
        for lb, lsts in by_type.items():
            pp = [l["price_val"] for l in lsts if l["price_val"]>0]
            ts[lb] = {"count":len(lsts),"min_price":_fmt(min(pp)) if pp else "없음",
                      "max_price":_fmt(max(pp)) if pp else "없음",
                      "min_price_val":min(pp) if pp else 0,
                      "max_price_val":max(pp) if pp else 0,
                      "best5_listings":lsts[:5]}
        return {"total_count":len(deduped),
                "min_price":_fmt(min(all_p)) if all_p else "없음",
                "max_price":_fmt(max(all_p)) if all_p else "없음",
                "best5":best5, "by_type":ts}
    except Exception as e:
        return {"error":str(e),"total_count":0}

# ============================================================
# 5. 국토부 실거래가 (36개월 병렬)
# ============================================================
_MOLIT = ("http://openapi.molit.go.kr:8081/OpenAPI_ToolInstallPackage"
          "/service/rest/RTMSOBJSvc/getRTMSDataSvcAptTrade")

def _fetch_month(lawd, ym, name):
    try:
        r = requests.get(_MOLIT, params={"serviceKey":_molit_key(),"LAWD_CD":lawd,
                                          "DEAL_YMD":ym,"numOfRows":"100","pageNo":"1"}, timeout=6)
        if r.status_code != 200: return []
        root = ET.fromstring(r.text); ml = min(3,len(name)); res = []
        for item in root.findall(".//item"):
            nm = item.findtext("단지명","").strip()
            if name[:ml] not in nm and nm[:ml] not in name: continue
            try: amt = int(item.findtext("거래금액","0").replace(",","").strip())
            except: amt = 0
            area = item.findtext("전용면적","0").strip()
            yr = item.findtext("년","").strip(); mo = item.findtext("월","").strip().zfill(2)
            if amt > 0:
                res.append({"ym":f"{yr}.{mo}","price_val":amt,"price":_fmt(amt),
                            "area":area,"pyeong":_pyeong(area),"floor":item.findtext("층","").strip()})
        return res
    except: return []

@st.cache_data(show_spinner=False, ttl=3600)
def get_transactions(lawd: str, name: str) -> dict:
    if not lawd: return {"error":"지역코드 없음"}
    all_t = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for f in concurrent.futures.as_completed([ex.submit(_fetch_month,lawd,ym,name) for ym in _yms(36)]):
            all_t.extend(f.result())
    if not all_t: return {"error":"거래 데이터 없음"}
    all_t.sort(key=lambda x: x["ym"], reverse=True)
    by_p = {}
    for t in all_t: by_p.setdefault(t["pyeong"],[]).append(t)
    sm = {}
    for py, ts in by_p.items():
        pp = [t["price_val"] for t in ts]
        mo = {}
        for t in ts: mo.setdefault(t["ym"],[]).append(t["price_val"])
        trend = [{"ym":ym,"avg_val":sum(v)//len(v),"avg":_fmt(sum(v)//len(v)),"count":len(v)}
                 for ym in sorted(mo)]
        sm[py] = {"count":len(ts),"avg":_fmt(sum(pp)//len(pp)),
                  "min":_fmt(min(pp)),"max":_fmt(max(pp)),"trend":trend,"recent10":ts[:10]}
    ap = [t["price_val"] for t in all_t]
    return {"total_count":len(all_t),"overall_avg":_fmt(sum(ap)//len(ap)),"by_pyeong":sm}

# ============================================================
# 6. GPT: 투자 분석
# ============================================================
@st.cache_data(show_spinner=False)
def analyze(cname,addr,hh,yr,l_sum,t_sum,q) -> dict:
    prompt = f"""
[단지] {cname} / {addr} / {hh}세대 / {yr}년
[매물] {l_sum}
[실거래] {t_sum}

반드시 아래 JSON 형식으로만 응답하세요:
{{"target_summary":"단지 한 줄 요약","market_assessment":"호가 vs 실거래 분석(3~4문장)","by_type_analysis":"평형별 가치 비교","investment_outlook":"매수 판단(3문장)","caution_points":"리스크 1~2가지"}}
"""
    try:
        resp = _openai().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"10년 경력 부동산 애널리스트. 데이터 기반, 솔직하게, JSON만 반환."},
                {"role":"user","content":f"'{q}' 분석:\n{prompt}"},
            ],
            temperature=0.2, max_tokens=1000,
        )
        return _parse_json(resp.choices[0].message.content)
    except Exception as e:
        return {"error":str(e)}

# ============================================================
# 7. UI
# ============================================================
st.set_page_config(page_title="PropTech AI 부동산 애널리스트", page_icon="🏘️", layout="wide")
st.markdown("""
<style>
.listing-row{border-left:3px solid #3d9970;padding:8px 12px;margin:5px 0;background:#f9fdf9;border-radius:0 6px 6px 0}
.txn-row{border-left:3px solid #0074d9;padding:6px 10px;margin:4px 0;font-size:.88em;background:#f5f8fd;border-radius:0 6px 6px 0}
</style>
""", unsafe_allow_html=True)

st.title("🏘️ PropTech AI 부동산 브리핑")
st.markdown("단지명, 주소, 필지명, 재개발 구역명 등 **어떤 형태로든** 입력하세요.")

ci, cb = st.columns([5,1])
with ci: q = st.text_input("검색", label_visibility="collapsed",
                            placeholder="예: 은마아파트 / 반포 1구역 / 압구정 현대 / 헬리오시티", key="q")
with cb: go = st.button("🔍 분석", type="primary", use_container_width=True)
st.divider()

if (go or q) and q:
    if st.session_state.get("_last") == q and not go: st.stop()
    st.session_state["_last"] = q

    with st.status("AI 분석 중...", expanded=True) as sb:
        st.write("🧠 단지명 추론 (GPT-4o-mini)...")
        keyword, reasoning = resolve_name(q)
        st.write(f"🔍 '{keyword}' 네이버 검색...")
        info = search_complex(keyword) or search_complex(q)

        if not info:
            sb.update(label="단지를 찾을 수 없습니다", state="error")
            st.error(f"**'{q}'** 단지를 찾을 수 없습니다.\n\n"
                     "💡 네이버 부동산에서 검색되는 정확한 단지명으로 다시 시도해보세요.\n"
                     f"예: [네이버에서 직접 검색](https://new.land.naver.com/search?sk={urllib.parse.quote(q)})")
            st.stop()

        st.write(f"📡 '{info['complex_name']}' 매물 수집 (중복제거)...")
        listings = get_listings(info["complex_no"])

        lawd = _lawd(info["address"])
        st.write(f"📋 국토부 실거래가 36개월 조회... (지역코드: {lawd or '미지원'})")
        txns = get_transactions(lawd, info["complex_name"]) if lawd else {"error":"지역코드 없음"}

        st.write("🤖 GPT-4o-mini 투자 분석...")
        l_lines = [f"매물 {listings.get('total_count',0)}건 | 호가 {listings.get('min_price','?')}~{listings.get('max_price','?')}"]
        for tn,td in listings.get("by_type",{}).items():
            l_lines.append(f"  [{tn}] {td['count']}건 | {td['min_price']}~{td['max_price']}")
        t_lines = []
        if "error" not in txns:
            t_lines.append(f"총 {txns.get('total_count',0)}건 / 평균 {txns.get('overall_avg','')}")
            for py,d in txns.get("by_pyeong",{}).items():
                t_lines.append(f"  [{py}] {d['count']}건 | 평균{d['avg']} | {d['min']}~{d['max']}")
        else:
            t_lines.append("실거래가 없음")
        ai = analyze(info["complex_name"],info["address"],info["total_households"],info["completion_year"],
                     "\n".join(l_lines),"\n".join(t_lines),q)
        sb.update(label="분석 완료! ✅", state="complete", expanded=False)

    naver_link = f"https://new.land.naver.com/complexes/{info['complex_no']}"
    h1,h2,h3 = st.columns([4,1,1])
    with h1:
        st.subheader(f"🏢 {info['complex_name']}")
        st.caption(f"📍 {info['address']}")
        if reasoning: st.caption(f"💡 AI 추론: {reasoning}")
    with h2: st.metric("세대수", f"{info['total_households']}세대" if info['total_households'] else "–")
    with h3: st.metric("입주연도", info['completion_year'] or "–")
    st.link_button("👉 네이버 부동산에서 전체 매물 보기", naver_link, type="secondary")
    st.divider()

    t1,t2,t3 = st.tabs(["🛒 현재 매물 (호가)","📊 실거래가 (3년)","🤖 AI 투자 분석"])

    with t1:
        if listings.get("total_count",0) == 0:
            st.warning("네이버 매물 데이터를 불러올 수 없습니다.")
        else:
            m1,m2,m3 = st.columns(3)
            m1.metric("총 매물 (중복제거)", f"{listings['total_count']}건")
            m2.metric("최저 호가", listings.get("min_price","–"))
            m3.metric("최고 호가", listings.get("max_price","–"))
            st.markdown("---")
            st.markdown("#### 🏆 전체 Best 5 (최저가 순)")
            for item in listings.get("best5",[]):
                ft = item.get("feature","")
                st.markdown(
                    f'<div class="listing-row"><strong>#{item["rank"]} 매매 {item["price"]}</strong>'
                    f' &nbsp;|&nbsp; {item.get("type","")} &nbsp;|&nbsp; '
                    f'{item["dong"]} {item["floor"]} &nbsp;|&nbsp; {item["direction"]}'
                    f' &nbsp;|&nbsp; <span style="color:#777">{(ft[:30]+"…") if len(ft)>32 else ft}</span></div>',
                    unsafe_allow_html=True)
            st.markdown("---")
            st.markdown("#### 📐 평형별 상세")
            for tn in sorted(listings.get("by_type",{}).keys(),
                             key=lambda t: int(t.split("평")[0]) if t[0].isdigit() else 0):
                td = listings["by_type"][tn]
                with st.expander(f"{tn}  ·  {td['count']}건  ·  {td['min_price']} ~ {td['max_price']}"):
                    for i,l in enumerate(td.get("best5_listings",[])):
                        c1,c2,c3,c4 = st.columns([2,1.2,1,2])
                        c1.markdown(f"**매매 {l['price']}**")
                        c2.caption(f"📍 {l['dong']} {l['floor']}")
                        c3.caption(f"🧭 {l['direction']}")
                        ft = l.get("feature","")
                        c4.caption((ft[:30]+"…") if len(ft)>32 else ft)
                        if i < len(td["best5_listings"])-1: st.divider()

    with t2:
        if txns.get("error"):
            st.warning(f"실거래가 데이터 없음 — {txns['error']}")
        else:
            c1,c2 = st.columns(2)
            c1.metric("3년 총 거래", f"{txns['total_count']:,}건")
            c2.metric("전체 평균가", txns["overall_avg"])
            rows = []
            for py,d in txns.get("by_pyeong",{}).items():
                for pt in d.get("trend",[]):
                    rows.append({"날짜":pt["ym"],"평형":py,"평균가(만원)":pt["avg_val"]})
            if rows:
                df = pd.DataFrame(rows).pivot_table(index="날짜",columns="평형",values="평균가(만원)",aggfunc="mean").sort_index()
                st.markdown("#### 📈 평형별 실거래가 트렌드")
                st.line_chart(df, height=300)
            st.markdown("---")
            for py in sorted(txns.get("by_pyeong",{}).keys(),
                             key=lambda t: int(t.split("평")[0]) if t[0].isdigit() else 0):
                d = txns["by_pyeong"][py]
                with st.expander(f"{py}  ·  {d['count']}건  ·  평균 {d['avg']}  ·  {d['min']}~{d['max']}"):
                    for t in d.get("recent10",[]):
                        st.markdown(
                            f'<div class="txn-row">📅 {t["ym"]} &nbsp;|&nbsp; '
                            f'💰 <strong>{t["price"]}</strong> &nbsp;|&nbsp; '
                            f'{t["area"]}㎡ &nbsp;|&nbsp; {t["floor"]}층</div>',
                            unsafe_allow_html=True)

    with t3:
        if ai.get("error"):
            st.error(f"AI 분석 실패: {ai['error']}")
        else:
            st.info(f"🎯 **단지 요약**\n\n{ai.get('target_summary','')}")
            cl,cr = st.columns(2)
            with cl:
                st.warning(f"📊 **시세 분석**\n\n{ai.get('market_assessment','')}")
                st.success(f"💰 **투자 전망**\n\n{ai.get('investment_outlook','')}")
            with cr:
                st.info(f"📐 **평형별 가치**\n\n{ai.get('by_type_analysis','')}")
                st.error(f"⚠️ **리스크**\n\n{ai.get('caution_points','')}")
        st.caption("💡 AI 참고용 분석입니다. 실제 투자 시 현장 방문·전문가 상담을 권장합니다.")
