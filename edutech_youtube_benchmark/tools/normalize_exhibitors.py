"""전시 간판명 -> 유튜브 검색용 회사/브랜드명 추출.

간판명은 회사명이 아닌 경우가 25%다(제품명, 서술형 문구, 교재명).
그대로 검색하면 매칭이 대량 실패하므로 아래 순서로 후보를 뽑는다.

  1) 따옴표·괄호로 강조된 브랜드      '하와이매쓰 AI', "유니냅", <고등둥심포니>
  2) 구분자(/, |, -)로 나뉜 조각      에이알쿠키 / (주)아이씨에프
  3) 쉼표 뒤 꼬리                    AI 국어 학습자료 생성, '꾸거'
  4) 법인 표기 제거                   (주)아이씨에프 -> 아이씨에프
  5) 영문명                          별칭으로만 사용 (검색 쿼터를 더 쓰지 않음)

대표 검색어는 '서술형 수식어가 걷힌 가장 짧은 고유명'을 고른다. 나머지는
전부 별칭으로 남겨 채널명 유사도 채점에 쓰인다.
"""
import csv, re

CORP = re.compile(
    r"\((주|유|재|사)\)|주식회사|㈜|"
    r"\b(?:Co\.?,?\s*Ltd\.?|Inc\.?|Corp(?:oration)?\.?|Ltd\.?|LLC)\b\.?",
    re.I,
)
# 서술형 수식어 — 붙어 있으면 회사명이 아니다.
# '솔루션/플랫폼' 등은 앞에 공백이 있을 때만 잡는다. 붙여 쓴 '엘앤피솔루션',
# '올인원장'은 정상 사명이므로 잘라내면 안 된다.
DESCRIPTIVE = re.compile(
    r"기반|맞춤|통합|전용|특화|활용|위한|하는|되는|잇는|기르는|만드는|깨우치는|키우는|"
    r"해결|생성|지원|교재|"
    r"(?:^|\s)(?:솔루션|플랫폼|서비스|프로그램|앱|어플|툴|프로젝트)$|"
    r"초·중·고|유초등|고등학교 대상|학교 맞춤|Seminar|Solution$|Platform$"
)
QUOTES = "'\"\u2018\u2019\u201c\u201d\u300c\u300d<>"
QUOTED = re.compile("[" + re.escape(QUOTES) + "]([^" + re.escape(QUOTES) + "]{2,20})[" + re.escape(QUOTES) + "]")
# 괄호 안은 부연설명인 경우가 많아 바깥쪽을 우선 후보로 삼는다
PAREN = re.compile(r"[(（]([^)）]{2,30})[)）]")
SPLIT = re.compile(r"\s*[/|_]\s*|\s+[-\u2013\u2014]\s+|\s*\+\s*")

def strip_corp(s: str) -> str:
    return CORP.sub("", s).strip(" .,·-")

def candidates(ko: str, en: str) -> list[str]:
    out = []
    for text in (ko, en):
        if not text:
            continue
        out += [m.strip() for m in QUOTED.findall(text)]          # ① 따옴표 브랜드
        # ①-b 괄호 바깥 부분을 우선 후보로 (괄호가 부연설명인 경우가 많다)
        if PAREN.search(text):
            outside = PAREN.sub("", text).strip(" .,·-")
            if outside:
                out.append(strip_corp(outside))
            out += [m.strip() for m in PAREN.findall(text)]
        for piece in SPLIT.split(PAREN.sub("", text) if PAREN.search(text) else text):
            piece = QUOTED.sub(r"\1", piece).strip(" .,·-")
            if not piece:
                continue
            out.append(strip_corp(piece))
            if "," in piece:                                       # ③ 쉼표 뒤 꼬리
                out.append(strip_corp(piece.rsplit(",", 1)[-1].strip()))
    # 정리: 빈 값·너무 짧은 값 제거, 순서 유지 중복 제거
    seen, clean = set(), []
    for c in out:
        c = re.sub(r"\s+", " ", c).strip(" .,·-()")
        if len(c) < 2 or c.lower() in seen:
            continue
        seen.add(c.lower())
        clean.append(c)
    return clean

def pick_primary(cands: list[str], ko: str) -> tuple[str, bool]:
    """대표 검색어와 '확인 필요' 플래그를 고른다."""
    # 서술형 수식어가 없는 후보를 우선. 그중 한글이 있는 짧은 것.
    clean = [c for c in cands if not DESCRIPTIVE.search(c)]
    pool = clean or cands
    if not pool:
        return ko, True
    # "브랜드 / (주)법인" 구조면 브랜드 쪽을 대표로 삼는다.
    # 유튜브 채널은 법인명(캐모릭스)보다 서비스 브랜드(프리마인드)로 개설된다.
    brand_side = ""
    if re.search(r"[/|]", ko):
        parts = [p.strip() for p in re.split(r"[/|]", ko) if p.strip()]
        plain = [p for p in parts if not CORP.search(p)]
        marked = [p for p in parts if CORP.search(p)]
        if plain and marked:
            brand_side = strip_corp(plain[0]).strip(" .,·-")
        elif len(parts) > 1 and not marked:
            # 법인 표기가 없으면 앞 조각을 브랜드로 본다
            # ("허밍블럭스 / 네모감성" -> 허밍블럭스)
            brand_side = strip_corp(parts[0]).strip(" .,·-")

    def rank(c):
        has_ko = bool(re.search(r"[가-힣]", c))
        is_brand = brand_side and c.strip().lower() == brand_side.lower()
        # 원문이 서술형일 때만 '원문 전체'를 후순위로 민다.
        # 원문이 곧 사명인 행까지 밀면 멀쩡한 한글 사명이 영문으로 바뀐다.
        is_whole = c.strip() == ko.strip() and bool(DESCRIPTIVE.search(ko))
        return (not is_brand, is_whole, not has_ko, len(c))
    best = sorted(pool, key=rank)[0]
    # 대표어가 원문과 많이 다르거나 후보가 여러 개면 사람이 한 번 봐야 한다
    needs_check = bool(DESCRIPTIVE.search(ko)) or len(ko) > 14 or not clean
    return best, needs_check

# 규칙으로 깔끔히 안 떨어지는 건들. 간판명 -> (대표검색어, 추가별칭)
OVERRIDES = {
    "학교 맞춤 생성형 AI 스쿨작(School作)": ("스쿨작", ["SCHOOLZAG", "School作"]),
    "강치친구클럽 캐릭터의 \"독도주간 지식+정서돌봄 교재\"": ("강치친구클럽", []),
    "엘팩토리(스마트갤러리 블루캔버스)": ("엘팩토리", ["블루캔버스", "ELFACTORY", "BLUECANVAS"]),
    "AI 롤플레잉, TIPP(팁) | 팁코퍼레이션": ("팁코퍼레이션", ["TIPP", "TIPP Corporation"]),
    "AI기반 수업관리솔루션 U-Class": ("U-Class", []),
    "피지컬AI_네오3D솔루션": ("네오3D솔루션", ["NEO 3D SOLUTION"]),
    "AI를 3D로 체감 하는 MOCOM": ("MOCOM", []),
    "묻는 힘을 기르는 AI 독서인문교육 · 소셜마인드": ("소셜마인드", ["SOCIAL MIND"]),
    "서울대학교 기술지주 자회사 앱티마이저": ("앱티마이저", ["Aptimizer"]),
    "이동KOTRA 수출애로상담관": ("KOTRA", []),
    "서술형·논술형·면접 특화 솔루션 카리": ("GOATHEAVEN", ["카리"]),
    "AI와 함께 만드는 정보 교과 프로젝트": ("Vibe Block", ["AKEO edu", "바이브블록"]),
    "AI 수업 생성·실습·운영 통합 플랫폼, Makit AI Learning OS": ("Makit AI", ["Makit AI Labs", "Makit AI Learning OS"]),
    "AI 특화 교육 플랫폼 / 내스타일": ("내스타일", ["GoodPrompt EDU", "굿프롬프트"]),
    "Seminar on Emerging Tech/AI응용디자인(서울대학교) 지원기업 폼유 + 데이터핀 + 에고이드":
        ("데이터핀", ["폼유", "에고이드", "FORMU", "DataPin", "Egoid"]),
    "건국대학교 매치업융합인재양성사업단": ("건국대학교 매치업사업단", ["건국대학교"]),
    "올인원 SW·AI 교육솔루션, 코드모스 AI": ("코드모스", ["CODMOS", "로지브라더스"]),
    "로지브라더스": ("로지브라더스", ["LOGIBROTHERS", "코드모스"]),
    "학생 맞춤형 생기부 '하마룸'·교·수·평·기 일체화 '하마오'": ("하마룸", ["하마오", "HAMAROOM", "HAMAO"]),
    "교·수·평·기 일체화 '하마오'": ("하마오", ["하마룸", "HAMAO", "HAMAROOM"]),
    "내 손안의 디지털 배지, 써티": ("써티", ["Certi"]),
    "음악교육 <고등둥심포니>": ("고등둥심포니", ["GDD SYMPHONY"]),
    "AI & XR 디지털 교실 솔루션 \"톡톡박스\"": ("톡톡박스", ["TokTokBox"]),
    "에듀테크 정보·체험 플랫폼 '에듀집'": ("에듀집", ["Edzip"]),
    # 대표어가 일반명사로 잡혀 검색이 불가능한 건들
    "LEIA - AI 코스웨어": ("LEIA", []),
    "전자칠판 거치대는 보인": ("보인", ["BOIN"]),
    "초등 영어·수학은 알공": ("알공", ["argong"]),
    "지정정보처리장치 S2B": ("S2B", []),
    "AI 코스웨어 오르조&체리": ("오르조", ["체리", "Orzo", "Cherry"]),
    "(주)추론 / 꾸럼e": ("꾸럼e", ["추론", "CHOORON"]),
    "솔트룩스이노베이션 X 딥엘": ("솔트룩스이노베이션", ["딥엘", "DeepL", "Saltlux"]),
    "EBS미디어 Ai영어쌤": ("EBS미디어", ["Ai영어쌤", "EBS"]),
    "KT & 코딩엑스": ("코딩엑스", ["CodingX", "KT", "AICE"]),
    "교풀AI / 휴몬랩": ("교풀AI", ["휴몬랩", "HuemoneLab", "Gyopool"]),
    "비피랩 미래교육연구소": ("비피랩", ["BPLAB"]),
    "클래스팅 AI | CT": ("클래스팅", ["클래스팅 AI", "Classting"]),
}

rows = list(csv.DictReader(open("_merged.tsv", encoding="utf-8"), delimiter="\t"))
out = []
for r in rows:
    ko, en = r["간판명(국문)"].strip(), r["간판명(영문)"].strip()
    cands = candidates(ko, en)
    if ko in OVERRIDES:
        primary, extra = OVERRIDES[ko]
        merged, seen = [], set()
        for c in extra + cands:           # 예외표 별칭이 자동 후보와 겹칠 수 있다
            if c.lower() not in seen:
                seen.add(c.lower())
                merged.append(c)
        cands = merged
        check = False                     # 사람이 이미 확인한 건
    else:
        primary, check = pick_primary(cands, ko)
    aliases = [c for c in cands if c.lower() != primary.lower()][:6]
    out.append({
        "company_name": primary,
        "aliases": "|".join(aliases),
        "channel_url": "",
        "category": r["존"],
        "signage_ko": ko,
        "signage_en": en,
        "exhibit": r["전시내용"].strip(),
        "needs_check": "Y" if check else "",
    })

with open("companies_normalized.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0]))
    w.writeheader(); w.writerows(out)

check_n = sum(1 for r in out if r["needs_check"])
print(f"193행 정규화 완료 — 사람 확인 권장 {check_n}건\n")
print("=== 원문이 크게 바뀐 행 (검수 대상) ===")
for r in out:
    if r["needs_check"]:
        print(f"  {r['signage_ko'][:44]:46s} -> {r['company_name']:22s} [{r['aliases'][:38]}]")
