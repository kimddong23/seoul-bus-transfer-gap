"""사전등록 지표 A1~A5 산출과 민감도·검증기간 재현.

docs/kpi_registry.yaml v1 정의를 그대로 구현함. 원자료 1행(같은 날·같은 시간대·같은 하차→승차 정류장 쌍)을
비교 단위로 삼아 대상 카드와 일반 카드의 평균 환승시간 차이를 두 카드 건수 중 작은 값으로 가중 평균함.
9월로 자격 경로와 후보를 고르고 8월·10월 1~13일로 재현을 확인함. 05·07 추가 점검과 테스트도 이 모듈의 함수를 씀.

입력: data/interim/days/*.parquet(02_profile.py 산출), data/raw/서울시버스정류소위치정보(20260902).xlsx,
      data/raw/자치구정류장코드분류.xlsx
출력: outputs/tables/ 아래 kpi_results.csv, a1_all.csv, candidates_{장애인,경로}.csv,
      eligible_paths_{장애인,경로}.csv, sensitivity.csv, holdout.csv, kpi_summary_for_report.json
실행: python scripts/04_kpis.py
"""

import hashlib
import json
import subprocess
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAYS = ROOT / "data" / "interim" / "days"
STOPS = ROOT / "data" / "raw" / "서울시버스정류소위치정보(20260902).xlsx"
GU = ROOT / "data" / "raw" / "자치구정류장코드분류.xlsx"  # OA-21231 첨부: 자치구 ↔ 정류장ID 앞 3자리
TAB = ROOT / "outputs" / "tables"
GROUPS = ["일반", "어린이", "청소년", "경로", "장애인"]
PRIMARY, SECONDARY = ["장애인", "경로"], ["어린이", "청소년"]
PERIODS = {"main": ("20260901", "20260930"), "validation_prior": ("20260801", "20260831"),
           "validation_later": ("20261001", "20261013")}
# 주 설정과 민감도 그리드(kpi_registry.yaml sensitivity_grid). m: 경로-일 최소 가중치, days: 유효한 날 수,
# rec: 대상 카드 기록 수, tau: 반복률·후보 기준(분)
MAIN = {"m": 5, "days": 7, "rec": 30, "tau": 0.0}
GRID = {"m": [1, 5, 10], "days": [5, 7, 14], "rec": [10, 30, 50], "tau": [0.0, 2.0, 5.0]}
TOP_K = 10  # 후보 표시 상한(목표치 아님)
# 추가 점검(사전등록 외): 정류장 이름에 든 낱말로 주변 시설을 근사 표시. 실제 시설 위치 자료 아님
FACILITY = {"시장·상업": ["시장", "백화점", "마트", "쇼핑", "아울렛"], "의료·복지": ["병원", "의료원", "보건소", "복지관", "요양"]}
BOOT_N, SEED = 1000, 20261019


def cfg_hash(cfg: dict) -> str:
    """설정 사전의 SHA-256 앞 12자리. 산출 표에서 어떤 기준 조합의 결과인지 식별하는 데 씀."""
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]


def load_base(con, glob: str) -> None:
    """원자료를 행 단위 기본 표 base로 읽음.

    일반 카드와 그 밖의 카드 유형 하나 이상이 함께 기록된 행만 남김. 같은 키(날짜·하차ID·승차ID·시간대)가 여러 행으로
    나뉜 경우도 합치지 않고 행 단위로 둠(kpi_registry.yaml cell). ID 길이로 경로유형을 정함(4자리 = 역 S,
    9자리 = 정류장 B). BB는 같은 정류장(BB_same)과 다른 정류장(BB_diff)으로 나눔.
    유효성은 valid 열: V1(카드 유형별 건수 합 = 전체 건수), V2(전체 평균 = 유형별 평균의 건수 가중 평균 ±1초).

    Args:
        con: DuckDB 연결.
        glob: 읽을 Parquet 경로 또는 글롭 패턴.
    """
    nsum = " + ".join(f"n_{g}::INT" for g in GROUPS)
    wsum = " + ".join(f"n_{g}::DOUBLE * t_{g}::DOUBLE" for g in GROUPS)
    period = " ".join(f"WHEN date BETWEEN '{a}' AND '{b}' THEN '{p}'" for p, (a, b) in PERIODS.items())
    cols = ", ".join(f"n_{g}::INT AS n_{g}, t_{g}::INT AS t_{g}" for g in GROUPS)
    con.execute(f"""CREATE OR REPLACE TABLE base AS
        SELECT CASE {period} END AS period, date, alight_id AS a, board_id AS b, hour,
               CASE WHEN length(alight_id)=4 THEN 'S' ELSE 'B' END || CASE WHEN length(board_id)=4 THEN 'S' ELSE 'B' END
                 || CASE WHEN length(alight_id)=9 AND length(board_id)=9 THEN
                      CASE WHEN alight_id=board_id THEN '_same' ELSE '_diff' END ELSE '' END AS stratum,
               coalesce(n_all::INT = {nsum} AND abs(t_all::DOUBLE - ({wsum}) / nullif({nsum}, 0)) <= 1, false) AS valid,
               {cols}
        FROM read_parquet('{glob}')
        WHERE n_일반::INT > 0 AND ({" OR ".join(f"n_{g}::INT > 0" for g in GROUPS[1:])})""")


def matched(con, g: str, use_validity: bool = True) -> None:
    """대상 카드 g의 대응 행을 경로-일로 묶은 표 pd를 만듦.

    대응 행은 n_g ≥ 1이고 n_일반 ≥ 1인 행. 시간 합계는 정수 초로 더해 병렬 집계의 합산 순서와 무관하게
    매번 같은 값이 나오게 함. 분 변환(/60)은 출력 단계에서만 함.
    pd 열: S(가중치 합 = 지지량), D(Σ 가중치×차이, 초), ng·nr(대상·일반 카드 건수 합),
    WTg·WTr(가중 평균시간용 합), n_rows(행 수), d_sum·ngd(보조 통계용 비가중·n_g 가중 차이 합).

    Args:
        con: load_base()를 마친 DuckDB 연결.
        g: 대상 카드 유형(GROUPS 중 하나).
        use_validity: False면 유효성 V1·V2를 적용하지 않음(민감도용).
    """
    where = f"n_{g} > 0" + (" AND valid" if use_validity else "")
    con.execute(f"""CREATE OR REPLACE TABLE rows_g AS
        SELECT period, date, a, b, stratum, n_{g} AS ng, n_일반 AS nr, least(n_{g}, n_일반) AS w,
               (t_{g} - t_일반)::BIGINT AS d, t_{g}::BIGINT AS tg, t_일반::BIGINT AS tr
        FROM base WHERE {where}""")
    con.execute("""CREATE OR REPLACE TABLE pd AS
        SELECT period, a, b, stratum, date, sum(w) AS S, sum(w * d) AS D, sum(ng) AS ng, sum(nr) AS nr,
               sum(w * tg) AS WTg, sum(w * tr) AS WTr, count(*) AS n_rows,
               sum(d) AS d_sum, sum(ng * d) AS ngd
        FROM rows_g GROUP BY ALL""")


def stratum_sql(stratum: str | None) -> str:
    """경로유형 조건 SQL 조각. None이면 조건 없음, 'BB'는 BB_same과 BB_diff를 합친 버스→버스 전체."""
    if not stratum:
        return ""
    return "AND stratum LIKE 'BB%'" if stratum == "BB" else f"AND stratum = '{stratum}'"


def a1(con, period: str, m: int, stratum: str | None = None) -> dict:
    """기간·경로유형별 A1(가중 평균 차이, 분)과 보조 통계.

    Args:
        con: matched()를 마친 DuckDB 연결.
        period: PERIODS 키.
        m: 경로-일 최소 가중치. 하루 가중치 합 S ≥ m인 경로-일(유효한 날)만 씀.
        stratum: 경로유형. None이면 전체.

    Returns:
        value(A1, 분), 분자·분모, 표본 수, 보조 통계(카드별 가중 평균시간, 경로-일 차이 중앙값,
        비가중 행 평균, n_g 가중 평균)를 담은 사전. 분모가 0이면 value는 None.
    """
    s = stratum_sql(stratum)
    r = con.execute(f"""SELECT sum(D) / 60, sum(S), count(*), count(DISTINCT date), count(DISTINCT (a, b)), sum(ng), sum(n_rows),
            sum(WTg) / sum(S) / 60, sum(WTr) / sum(S) / 60, median(D / S) / 60, sum(d_sum) / sum(n_rows) / 60,
            sum(ngd) / sum(ng) / 60
        FROM pd WHERE period = '{period}' AND S >= {m} {s}""").fetchone()
    keys = ["numerator", "denominator", "n_pathdays", "n_days", "n_paths", "n_records", "n_rows",
            "tbar_g", "tbar_r", "median_pathday", "unweighted_row_mean", "ng_weighted_mean"]
    out = dict(zip(keys, r))
    out["value"] = out["numerator"] / out["denominator"] if out["denominator"] else None
    return out


def bootstrap(con, period: str, m: int, stratum: str | None = None) -> tuple[float, float]:
    """날짜 단위 군집 부트스트랩 95% 구간(분).

    날짜를 복원 추출해 Σ D / Σ S를 다시 구함. 날짜순으로 정렬한 뒤 고정 시드로 뽑으므로 실행마다 같은 구간이 나옴.
    인자는 a1()과 같음.

    Returns:
        (2.5%, 97.5%) 백분위. 날짜가 2개 미만이면 (None, None).
    """
    d = con.execute(f"""SELECT date, sum(D), sum(S) FROM pd WHERE period='{period}' AND S >= {m} {stratum_sql(stratum)}
        GROUP BY date ORDER BY date""").fetchnumpy()
    D, S = d["sum(D)"], d["sum(S)"]
    if len(D) < 2:
        return (None, None)
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(D), size=(BOOT_N, len(D)))
    est = D[idx].sum(1) / S[idx].sum(1) / 60
    return tuple(float(x) for x in np.percentile(est, [2.5, 97.5]))


def bootstrap_paths(con, period: str, m: int, stratum: str | None = None) -> tuple[float, float]:
    """경로 단위 군집 부트스트랩 95% 구간(분). 추가 점검(사전등록 외)의 보조 구간.

    경로를 복원 추출함. 같은 경로의 여러 날이 서로 독립이 아닐 수 있어 날짜 단위 구간과 함께 확인함.
    정렬·시드·반환값은 bootstrap()과 같음.
    """
    d = con.execute(f"""SELECT a, b, sum(D), sum(S) FROM pd WHERE period='{period}' AND S >= {m} {stratum_sql(stratum)}
        GROUP BY a, b ORDER BY a, b""").fetchnumpy()
    D, S = d["sum(D)"].astype(float), d["sum(S)"].astype(float)
    if len(D) < 2:
        return (None, None)
    rng = np.random.default_rng(SEED)
    est = np.array([D[i].sum() / S[i].sum() / 60 for i in rng.integers(0, len(D), size=(BOOT_N, len(D)))])
    return tuple(float(x) for x in np.percentile(est, [2.5, 97.5]))


def facility(*names: str) -> str:
    """정류장 이름들에서 찾은 FACILITY 분류를 '·'로 이어 붙임. 해당 없으면 빈 문자열."""
    hits = [k for k, words in FACILITY.items() if any(isinstance(n, str) and w in n for n in names for w in words)]
    return "·".join(hits)


def paths(con, period: str, m: int, tau: float) -> pd.DataFrame:
    """기간 안 경로별 집계(A2·A3·A4의 기본 표).

    Args:
        con: matched()를 마친 DuckDB 연결.
        period: PERIODS 키.
        m: 경로-일 최소 가중치.
        tau: 반복률 기준(분). 하루 차이가 tau분을 넘는 유효한 날의 비율이 R.

    Returns:
        경로별 days(유효한 날 수), N(대상 카드 기록 수), M(경로 차이, 분), S·D_s(가중치 합, Σ 가중치×차이 초),
        R(반복률, %), Tg·Tr(대상·일반 카드 가중 평균 환승시간, 분) 표.
    """
    return con.execute(f"""SELECT a, b, stratum, count(*) AS days, sum(ng) AS N, sum(D) / sum(S) / 60 AS M, sum(S) AS S,
            sum(D) AS D_s, 100.0 * sum((D > {tau * 60} * S)::INT) / count(*) AS R,
            sum(WTg) / sum(S) / 60 AS Tg, sum(WTr) / sum(S) / 60 AS Tr
        FROM pd WHERE period = '{period}' AND S >= {m} GROUP BY a, b, stratum ORDER BY a, b""").df()


def pareto_layers(M: np.ndarray, N: np.ndarray) -> np.ndarray:
    """두 축 모두 클수록 좋은 파레토 비지배 층 번호(1부터)."""
    layer = np.zeros(len(M), dtype=int)
    left = np.arange(len(M))
    k = 0
    while len(left):
        k += 1
        m, n = M[left], N[left]
        dominated = ((m[None, :] >= m[:, None]) & (n[None, :] >= n[:, None])
                     & ((m[None, :] > m[:, None]) | (n[None, :] > n[:, None]))).any(1)
        layer[left[~dominated]] = k
        left = left[dominated]
    return layer


def candidates(p: pd.DataFrame, days: int, rec: int, tau: float) -> pd.DataFrame:
    """자격 경로에서 후보를 골라 파레토 층 → 경로 차이 M 순으로 정렬(A4).

    M 비교는 정수 초 합(D_s > tau·60·S)으로 해 나눗셈 반올림의 영향을 받지 않음.

    Args:
        p: paths()가 만든 경로별 집계 표.
        days: 자격 경로의 최소 유효한 날 수.
        rec: 자격 경로의 최소 대상 카드 기록 수(N).
        tau: 후보 기준(분). M > tau인 자격 경로가 후보.

    Returns:
        후보 표. p의 열에 파레토 층 번호 layer를 더하고 layer 오름차순, M 내림차순으로 정렬함.
        후보가 없으면 빈 표.
    """
    e = p[(p.days >= days) & (p.N >= rec) & (p.D_s > tau * 60 * p.S)].copy()
    e["layer"] = pareto_layers(e.M.to_numpy(), e.N.to_numpy().astype(float)) if len(e) else []
    return e.sort_values(["layer", "M"], ascending=[True, False]).reset_index(drop=True)


def stop_master() -> pd.DataFrame:
    """서울시 버스정류소 위치정보(OA-15067, 2026-09-02 기준). NODE_ID = 원자료 9자리 정류장 ID."""
    st = pd.read_excel(STOPS, dtype=str)
    return st.rename(columns={"NODE_ID": "id", "ARS_ID": "ars", "정류소명": "name", "X좌표": "x", "Y좌표": "y",
                              "정류소타입": "stop_type"}).drop_duplicates("id").set_index("id")


def name_paths(df: pd.DataFrame, st: pd.DataFrame) -> pd.DataFrame:
    """하차(a_)·승차(b_) 정류장의 이름·ARS 번호·좌표를 붙임. 역 ID는 위치 자료에 없어 빈칸."""
    for end in ("a", "b"):
        df = df.join(st[["name", "ars", "x", "y"]].add_prefix(f"{end}_"), on=end)
    return df


def main() -> None:
    """사전등록 지표·민감도·검증기간 결과를 모두 계산해 outputs/tables에 저장."""
    TAB.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    load_base(con, f"{DAYS}/*.parquet")
    st = stop_master()
    con.register("stops", st.reset_index()[["id"]])
    # 산출물 method_version에 코드 커밋을 남겨 어떤 코드로 만든 결과인지 추적
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    method = f"registry_v1@{commit}"
    main_hash = cfg_hash(MAIN)
    rows, summary, sens, hold, a1_rows = [], {"method_version": method, "config": MAIN, "config_hash": main_hash}, [], [], []

    def kpi_row(kpi, g, loc, period, value, unit, num=None, den=None, pairs=None, days=None, rec=None,
                status="OK", lim="", cfg=None):
        a, b = PERIODS[period]
        rows.append({"kpi_id": kpi, "group_code": g, "location_id": loc, "period_start": a, "period_end": b,
                     "value": value, "unit": unit, "numerator": num, "denominator": den, "n_valid_pairs": pairs,
                     "n_valid_days": days, "n_records": rec, "method_version": method,
                     "threshold_config_hash": cfg or main_hash, "source_manifest_ref": "outputs/validation/download_manifest.csv",
                     "status": status if value is not None else "NA", "limitations": lim})

    coverage = {}
    for g in PRIMARY + SECONDARY:
        summary[g] = {}
        for validity in (True, False):
            matched(con, g, validity)
            for period in PERIODS:
                for m in GRID["m"]:
                    for stratum in [None, "BB", "BB_same", "BB_diff", "BS", "SB", "SS"]:
                        # 세부 경로유형은 주 설정(m=5, 유효성 적용)에서만. 전체·BB는 모든 m·유효성 조합(민감도용)
                        if stratum and stratum != "BB" and (m != MAIN["m"] or not validity):
                            continue
                        r = a1(con, period, m, stratum)
                        lo, hi = bootstrap(con, period, m, stratum)
                        plo, phi = (bootstrap_paths(con, period, m, stratum)
                                    if validity and m == MAIN["m"] and stratum in (None, "BB") else (None, None))
                        a1_rows.append({"group": g, "validity": validity, "period": period, "m": m,
                                        "stratum": stratum or "ALL", "ci_lo": lo, "ci_hi": hi,
                                        "ci_path_lo": plo, "ci_path_hi": phi, **r})
                        if validity and m == MAIN["m"] and stratum is None:
                            kpi_row("A1", g, "SEOUL_ALL", period, r["value"], "분", r["numerator"], r["denominator"],
                                    r["n_pathdays"], r["n_days"], r["n_records"],
                                    lim="관측 차이, 인과·차별·시설결함 아님. 카드 분류 ≠ 장애·연령 전수")
                        if validity and m == MAIN["m"] and period == "main" and stratum:
                            kpi_row("A1", g, f"STRATUM_{stratum}", period, r["value"], "분", r["numerator"],
                                    r["denominator"], r["n_pathdays"], r["n_days"], r["n_records"])
            if not validity or g not in PRIMARY:
                continue
            # A5 커버리지(주 기간)
            cv = con.execute(f"""SELECT
                  (SELECT count(*) FROM base WHERE period='main' AND n_{g}>0) AS rows_matched_all,
                  (SELECT count(*) FROM base WHERE period='main' AND n_{g}>0 AND valid) AS rows_matched_valid,
                  (SELECT sum(least(n_{g}, n_일반)) FROM base WHERE period='main' AND n_{g}>0 AND valid) AS w_valid,
                  (SELECT sum(S) FROM pd WHERE period='main' AND S >= {MAIN['m']}) AS w_in_valid_pathdays,
                  (SELECT sum(ng) FROM pd WHERE period='main' AND S >= {MAIN['m']}) AS ng_in_valid_pathdays""").fetchone()
            a, b = PERIODS["main"]
            tot_g = con.execute(f"SELECT sum(n_{g}::BIGINT) FROM read_parquet('{DAYS}/*.parquet') "
                                f"WHERE date BETWEEN '{a}' AND '{b}'").fetchone()[0]
            coverage[g] = {"rows_matched_all": cv[0], "rows_matched_valid": cv[1], "share_valid": cv[1] / cv[0],
                           "w_valid": cv[2], "w_in_valid_pathdays": cv[3], "share_w_in_valid_pathdays": cv[3] / cv[2],
                           "records_g_total_main": int(tot_g), "records_g_in_valid_pathdays": cv[4],
                           "share_records_g_used": cv[4] / tot_g}
            coverage[g]["matched_rows_by_stratum"] = dict(con.execute(
                f"SELECT stratum, count(*) FROM base WHERE period='main' AND n_{g}>0 GROUP BY 1 ORDER BY 1").fetchall())
            kpi_row("A5", g, "SEOUL_ALL", "main", 100 * coverage[g]["share_valid"], "%", cv[1], cv[0],
                    lim="(a) 유효성 통과 대응 행 비율")
            kpi_row("A5", g, "SEOUL_ALL", "main", 100 * coverage[g]["share_records_g_used"], "%", cv[4], int(tot_g),
                    lim="(c) 대상 집단 기록 중 유효 경로-일에 들어간 비율")

            # 경로 단위: A2·A3·A4 + 민감도 그리드
            prim = None
            for m in GRID["m"]:
                for tau in GRID["tau"]:
                    p = paths(con, "main", m, tau)
                    for days in GRID["days"]:
                        for rec in GRID["rec"]:
                            c = candidates(p, days, rec, tau)
                            cfg = {"m": m, "days": days, "rec": rec, "tau": tau}
                            elig = int(((p.days >= days) & (p.N >= rec)).sum())
                            top = list(zip(c.a, c.b))[:TOP_K]
                            if cfg == MAIN:
                                prim = (c, p, top)
                            sens.append({"group": g, **cfg, "config_hash": cfg_hash(cfg), "eligible_paths": elig,
                                         "candidates": len(c), "top_ids": ";".join(f"{a}>{b}" for a, b in top)})
            c, p, top = prim
            for s in sens:
                if s["group"] == g:
                    s["top10_overlap_with_main"] = len(set(s["top_ids"].split(";")) & {f"{a}>{b}" for a, b in top}) if s["top_ids"] else 0
            c = name_paths(c, st)
            c["facility"] = [facility(x, y) for x, y in zip(c.a_name, c.b_name)]
            gu = pd.read_excel(GU, dtype=str).set_index("정류장ID(앞3자리)")["자치구"]
            c["b_gu"] = [gu.get(str(x)[:3]) if len(str(x)) == 9 else None for x in c.b]
            for period, tag in (("validation_prior", "aug"), ("validation_later", "oct")):
                hp = paths(con, period, MAIN["m"], 0.0).set_index(["a", "b"])[["days", "M"]].add_suffix(f"_{tag}")
                c = c.join(hp, on=["a", "b"])
            c.to_csv(TAB / f"candidates_{g}.csv", index=False)
            elig = p[(p.days >= MAIN["days"]) & (p.N >= MAIN["rec"])]
            elig.to_csv(TAB / f"eligible_paths_{g}.csv", index=False)
            ids = set(elig.a[elig.a.str.len() == 9]) | set(elig.b[elig.b.str.len() == 9])
            hit = len(ids & set(st.index))
            coverage[g]["stop_master_match_eligible"] = [hit, len(ids)]
            kpi_row("A5", g, "SEOUL_ALL", "main", 100 * hit / len(ids) if ids else None, "%", hit, len(ids),
                    lim="(d) 자격 경로의 정류장 ID 중 정류소 위치정보(2026-09-02) 매칭 비율. 역 ID는 마스터 미확보로 미산출")
            for _, r in c.head(TOP_K).iterrows():
                loc = f"{r.a}>{r.b}"
                kpi_row("A4", g, loc, "main", r.M, "분", None, None, int(r.days), int(r.days), int(r.N),
                        lim=f"파레토 층 {r.layer}. 우선 확인 후보이지 시설결함 지점 아님")
                kpi_row("A2", g, loc, "main", r.R, "%", round(r.R * r.days / 100), int(r.days), int(r.days),
                        int(r.days), int(r.N))
            kpi_row("A4", g, "COUNT", "main", len(c), "경로", lim="자격 경로 중 M>0 후보 수(표시 상한 10은 목표 아님)")
            kpi_row("A3", g, "ELIGIBLE_PATHS", "main", len(elig), "경로")
            kpi_row("A2", g, "ELIGIBLE_MEDIAN_R", "main", float(elig.R.median()) if len(elig) else None, "%")

            # 검증기간(8월·10월 1~13일) 재현
            for period in ("validation_prior", "validation_later"):
                hp = paths(con, period, MAIN["m"], 0.0).set_index(["a", "b"])
                elig_bb = elig[elig.stratum.str.startswith("BB")]  # 버스→버스 자격 경로만(역이 낀 BS·SB·SS 제외)
                for label, setp in [("top10", c.head(TOP_K)), ("all_candidates", c), ("all_eligible", elig),
                                    ("all_eligible_bb", elig_bb)]:
                    keys = list(zip(setp.a, setp.b))
                    found = [k for k in keys if k in hp.index]
                    pos = sum(hp.loc[k, "D_s"] > 0 for k in found)
                    hold.append({"group": g, "period": period, "set": label, "n_set": len(keys),
                                 "n_with_valid_days": len(found), "n_positive": int(pos),
                                 "share_positive": pos / len(found) if found else None,
                                 "mean_M_main": float(setp.M.mean()) if len(setp) else None,
                                 "mean_M_holdout": float(hp.loc[found, "M"].mean()) if found else None})
                for label, base_set in (("spearman_eligible_both", elig), ("spearman_eligible_both_bb", elig_bb)):
                    both = base_set.set_index(["a", "b"]).join(
                        hp[(hp.days >= MAIN["days"]) & (hp.N >= MAIN["rec"])][["M"]], rsuffix="_h", how="inner")
                    rho = both.M.rank().corr(both.M_h.rank()) if len(both) > 2 else None
                    hold.append({"group": g, "period": period, "set": label, "n_set": len(both), "spearman": rho})
            summary[g]["candidates_top"] = c.head(TOP_K).to_dict("records")
            summary[g]["n_candidates"] = len(c)
            summary[g]["n_eligible"] = len(elig)
            summary[g]["eligible_R_median"] = float(elig.R.median()) if len(elig) else None
            summary[g]["eligible_share_M_pos"] = float((elig.D_s > 0).mean()) if len(elig) else None
            ebb = elig[elig.stratum.str.startswith("BB")]
            summary[g]["n_eligible_bb"] = len(ebb)
            summary[g]["eligible_bb_R_median"] = float(ebb.R.median()) if len(ebb) else None
            summary[g]["eligible_bb_share_M_pos"] = float((ebb.D_s > 0).mean()) if len(ebb) else None

    a1_df = pd.DataFrame(a1_rows)
    a1_df.to_csv(TAB / "a1_all.csv", index=False)
    pd.DataFrame(rows).to_csv(TAB / "kpi_results.csv", index=False)
    pd.DataFrame(sens).to_csv(TAB / "sensitivity.csv", index=False)
    pd.DataFrame(hold).to_csv(TAB / "holdout.csv", index=False)
    for g in PRIMARY + SECONDARY:
        sel = a1_df[(a1_df.group == g) & (a1_df.validity) & (a1_df.m == MAIN["m"])]
        summary[g]["A1"] = sel.where(sel.notna(), None).to_dict("records")
    a, b = PERIODS["main"]
    all_ids = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE id IN (SELECT id FROM stops)) FROM (
        SELECT alight_id AS id FROM read_parquet('{DAYS}/*.parquet') WHERE date BETWEEN '{a}' AND '{b}' AND length(alight_id)=9
        UNION SELECT board_id FROM read_parquet('{DAYS}/*.parquet') WHERE date BETWEEN '{a}' AND '{b}' AND length(board_id)=9)""").fetchone()
    coverage["stop_master_match_all_main"] = list(all_ids)
    summary["coverage"] = coverage
    with open(TAB / "kpi_summary_for_report.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(a1_df[(a1_df.validity) & (a1_df.m == MAIN["m"])][["group", "period", "stratum", "value", "ci_lo", "ci_hi",
                                                            "n_pathdays", "n_paths", "n_records"]].to_string())


if __name__ == "__main__":
    main()
