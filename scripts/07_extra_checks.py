"""버스→버스 A1과 후보의 추가 점검(사전등록 외).

결과 산출 뒤 더한 탐색적 점검임. 변경 이력은 docs/kpi_changelog.md #7·#8. 대응 행·가중치·유효한 날 정의는
04_kpis.py와 같고 별도 표시가 없으면 9월 버스→버스 환승만 씀.

점검 항목:
    1) 상한 기록 제외  2) 평일/주말·공휴일  3) 시간대  4) 같은 정류장 환승의 정류장 종류
    5) 9월 홀수일·짝수일 재현  6) 자격 경로별 차이 크기 분포  7) 상위 후보의 상한 기록 비율
    8) 정류장 이름에 '시장'이 든 경로와 그 외(전통시장 좌표 자료 대신 쓴 이름 기준 근사)
    9) 이름 기준 시설(04_kpis.FACILITY: 시장·상업, 의료·복지) 정류장 제외
    10) 시간대 분해: 주 분석의 유효 경로-일 행을 시간대로 나눔(시간대 값의 가중합 = 전체 값).
        일반 카드 대비 상대 차이, 8월·10월 포함
    11) 9월 1분 넘는 경로의 8월 재현, 경로별 월간 변동(9월−8월) 표준편차

입력: data/interim/days/*.parquet, data/raw/서울시버스정류소위치정보(20260902).xlsx,
      outputs/tables/eligible_paths_{장애인,경로}.csv·candidates_{장애인,경로}.csv(04_kpis.py 산출)
출력: outputs/tables/ 아래 extra_checks.csv, extra_split_half.csv, extra_magnitude.csv, extra_candidate_cap.csv,
      extra_timeband.csv, extra_repro.csv
실행: python scripts/07_extra_checks.py(04_kpis.py 다음)
"""

import importlib.util
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kpis", ROOT / "scripts" / "04_kpis.py")
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)

M = k.MAIN["m"]
# 시간대별 환승시간 상한(초): 07~20시 1,980초(33분), 그 밖 3,960초(66분). 원자료 최댓값과 같음
CAP = "CASE WHEN hour BETWEEN '07' AND '20' THEN 1980 ELSE 3960 END"
HOLIDAYS = ["20260924", "20260925", "20260926"]  # 2026 추석 연휴(9월 자료에서 행 수가 주말 수준으로 줄어든 날과 일치)
DAYTYPE = (f"CASE WHEN dayofweek(strptime(date, '%Y%m%d')) IN (0, 6) OR date IN ({','.join(repr(d) for d in HOLIDAYS)}) "
           "THEN '주말·공휴일' ELSE '평일' END")
FAC = "|".join(w for words in k.FACILITY.values() for w in words)
BAND = ("CASE WHEN hour BETWEEN '07' AND '09' THEN '출근(07~09시)' WHEN hour BETWEEN '10' AND '16' THEN '낮(10~16시)' "
        "WHEN hour BETWEEN '17' AND '19' THEN '퇴근(17~19시)' ELSE '저녁·심야(20~06시)' END")


def a1_where(con, g: str, where: str, group_expr: str = "'전체'") -> pd.DataFrame:
    """조건을 건 대응 행으로 경로-일을 다시 묶어 A1과 날짜 단위 부트스트랩 구간을 구함.

    Args:
        con: base_t를 만든 DuckDB 연결.
        g: 대상 카드 유형.
        where: 대응 행에 더할 SQL 조건.
        group_expr: 부분집합 이름을 만드는 SQL 식. 값마다 따로 계산함.

    Returns:
        부분집합별 value(분)·ci_lo·ci_hi·n_pathdays·n_records 표.
    """
    con.execute(f"""CREATE OR REPLACE TABLE r AS
        SELECT {group_expr} AS grp, date, a, b, least(n_{g}, n_일반) AS w, (t_{g} - t_일반)::BIGINT AS d, n_{g} AS ng
        FROM base_t WHERE period = 'main' AND stratum LIKE 'BB%' AND valid AND n_{g} > 0 AND ({where})""")
    con.execute("""CREATE OR REPLACE TABLE p AS
        SELECT grp, a, b, date, sum(w) AS S, sum(w * d) AS D, sum(ng) AS ng FROM r GROUP BY ALL""")
    out = []
    for grp in [x[0] for x in con.execute("SELECT DISTINCT grp FROM p ORDER BY 1").fetchall()]:
        v = con.execute(f"SELECT sum(D) / sum(S) / 60, count(*), sum(ng) FROM p WHERE grp = '{grp}' AND S >= {M}").fetchone()
        dd = con.execute(f"""SELECT date, sum(D), sum(S) FROM p WHERE grp = '{grp}' AND S >= {M}
                             GROUP BY date ORDER BY date""").fetchnumpy()
        ci = (None, None)
        if len(dd["date"]) > 1:
            idx = np.random.default_rng(k.SEED).integers(0, len(dd["date"]), (k.BOOT_N, len(dd["date"])))
            ci = tuple(np.percentile(dd["sum(D)"][idx].sum(1) / dd["sum(S)"][idx].sum(1) / 60, [2.5, 97.5]))
        out.append({"group": g, "subset": grp, "value": v[0], "ci_lo": ci[0], "ci_hi": ci[1], "n_pathdays": v[1],
                    "n_records": v[2]})
    return pd.DataFrame(out)


def main() -> None:
    """점검 1~11을 계산해 extra_*.csv로 저장."""
    con = duckdb.connect()
    k.load_base(con, f"{k.DAYS}/*.parquet")
    st = k.stop_master()
    con.register("stype", st.reset_index()[["id", "stop_type", "name"]])
    con.execute(f"""CREATE OR REPLACE TABLE base_t AS SELECT base.*, CASE WHEN s.stop_type LIKE '가로변%' THEN '가로변'
        ELSE coalesce(s.stop_type, '정보 없음') END AS stop_type,
        coalesce(s.name LIKE '%시장%', false) OR coalesce(s2.name LIKE '%시장%', false) AS market,
        coalesce(regexp_matches(s.name, '{FAC}'), false) OR coalesce(regexp_matches(s2.name, '{FAC}'), false) AS fac
        FROM base LEFT JOIN stype s ON s.id = base.a LEFT JOIN stype s2 ON s2.id = base.b""")
    rows = []
    for g in k.PRIMARY + k.SECONDARY:
        rows.append(a1_where(con, g, "true").assign(check="기준(버스→버스)"))
        rows.append(a1_where(con, g, f"t_{g} < {CAP} AND t_일반 < {CAP}").assign(check="상한 기록 제외"))
        rows.append(a1_where(con, g, "true", DAYTYPE).assign(check="요일"))
        rows.append(a1_where(con, g, "true", BAND).assign(check="시간대"))
        rows.append(a1_where(con, g, "a = b", "stop_type").assign(check="같은 정류장 환승의 정류장 종류"))
        rows.append(a1_where(con, g, "true", "CASE WHEN market THEN '시장 이름 정류장' ELSE '그 외' END")
                    .assign(check="정류장 이름에 '시장' 포함"))
        rows.append(a1_where(con, g, "true", "CASE WHEN fac THEN '시설 이름 정류장' ELSE '그 외' END")
                    .assign(check="정류장 이름 기준 시설(시장·상업·의료·복지)"))
    pd.concat(rows).to_csv(k.TAB / "extra_checks.csv", index=False)

    # 5) 홀수일·짝수일: 9월 자격 경로의 경로별 차이를 반으로 나눈 날짜에서 따로 계산
    half, mag, cap = [], [], []
    for g in k.PRIMARY:
        k.matched(con, g)
        e = pd.read_csv(k.TAB / f"eligible_paths_{g}.csv", dtype={"a": str, "b": str})
        e = e[e.stratum.str.startswith("BB")]  # 버스→버스 자격 경로만
        hp = con.execute(f"""SELECT a, b, (CAST(right(date, 2) AS INT) % 2) AS odd, sum(D) / sum(S) / 60 AS M, count(*) AS days
            FROM pd WHERE period = 'main' AND S >= {M} GROUP BY ALL""").df()
        w = hp.pivot_table(index=["a", "b"], columns="odd", values="M").dropna()
        both = e.set_index(["a", "b"]).join(w, how="inner")
        rho = both[0].rank().corr(both[1].rank())
        c = pd.read_csv(k.TAB / f"candidates_{g}.csv", dtype={"a": str, "b": str}).head(k.TOP_K)
        cand = c.set_index(["a", "b"]).join(w, how="left")
        half.append({"group": g, "n_paths_both_halves": len(both), "spearman_odd_even": rho,
                     "top10_pos_in_odd": int((cand[1] > 0).sum()), "top10_pos_in_even": int((cand[0] > 0).sum()),
                     "top10_with_both": int(cand[[0, 1]].notna().all(axis=1).sum())})
        # 6) 차이 크기 분포(자격 경로)
        for th in (0, 0.5, 1, 2):
            mag.append({"group": g, "threshold_min": th, "n_paths": int((e.M > th).sum()), "of_eligible": len(e),
                        "share": float((e.M > th).mean())})
        mag.append({"group": g, "threshold_min": "M>1 & R>=70", "n_paths": int(((e.M > 1) & (e.R >= 70)).sum()),
                    "of_eligible": len(e), "share": float(((e.M > 1) & (e.R >= 70)).mean())})
        # 7) 상위 후보의 상한 기록 비율(대상 카드 평균이 상한과 같은 대응 행의 대상 카드 건수 비율)
        for i, r in c.iterrows():
            x = con.execute(f"""WITH v AS (SELECT a, b, date FROM pd WHERE period='main' AND S >= {M} AND a='{r.a}' AND b='{r.b}')
                SELECT sum(n_{g}) FILTER (WHERE t_{g} >= {CAP}), sum(n_{g}) FROM base JOIN v USING (a, b, date)
                WHERE period='main' AND valid AND n_{g} > 0""").fetchone()
            cap.append({"group": g, "rank": i + 1, "a": r.a, "b": r.b, "records_at_cap": x[0] or 0, "records": x[1],
                        "share_at_cap": (x[0] or 0) / x[1] if x[1] else None})
    # 10) 시간대 분해(주 분석의 유효 경로-일에 속한 행만, 시간대별로 나눔)
    band_rows = []
    for g in k.PRIMARY + k.SECONDARY:
        k.matched(con, g)
        con.execute(f"""CREATE OR REPLACE TABLE rb AS
            SELECT t.period, t.date, {BAND} AS band, least(t.n_{g}, t.n_일반) AS w, (t.t_{g} - t.t_일반)::BIGINT AS d,
                   t.t_일반::BIGINT AS tr
            FROM base t JOIN (SELECT period, a, b, date FROM pd WHERE S >= {M} AND stratum LIKE 'BB%') v
              ON t.period = v.period AND t.a = v.a AND t.b = v.b AND t.date = v.date
            WHERE t.valid AND t.n_{g} > 0 AND t.stratum LIKE 'BB%'""")
        for per, band in con.execute("SELECT DISTINCT period, band FROM rb ORDER BY 1, 2").fetchall():
            v = con.execute(f"""SELECT sum(w * d) / sum(w) / 60, sum(w * tr) / sum(w) / 60, sum(w)
                FROM rb WHERE period = '{per}' AND band = '{band}'""").fetchone()
            dd = con.execute(f"""SELECT date, sum(w * d) AS sd, sum(w) AS sw FROM rb WHERE period = '{per}' AND band = '{band}'
                GROUP BY date ORDER BY date""").fetchnumpy()
            idx = np.random.default_rng(k.SEED).integers(0, len(dd["date"]), (k.BOOT_N, len(dd["date"])))
            est = dd["sd"][idx].sum(1) / dd["sw"][idx].sum(1) / 60
            lo, hi = np.percentile(est, [2.5, 97.5])
            band_rows.append({"group": g, "period": per, "band": band, "value": v[0], "ci_lo": lo, "ci_hi": hi,
                              "tbar_r": v[1], "relative_pct": 100 * v[0] / v[1], "weight": v[2]})
    pd.DataFrame(band_rows).to_csv(k.TAB / "extra_timeband.csv", index=False)

    # 11) 9월 1분 넘는 경로의 8월 재현, 경로별 월간 변동
    repro = []
    for g in k.PRIMARY:
        k.matched(con, g)
        e = pd.read_csv(k.TAB / f"eligible_paths_{g}.csv", dtype={"a": str, "b": str})
        e = e[e.stratum.str.startswith("BB")].set_index(["a", "b"])
        aug = k.paths(con, "validation_prior", M, 0.0).set_index(["a", "b"])
        big = e[e.M > 1].join(aug[["M", "days", "N"]], rsuffix="_aug", how="left")
        both = e.join(aug[(aug.days >= k.MAIN["days"]) & (aug.N >= k.MAIN["rec"])][["M"]], rsuffix="_aug", how="inner")
        diff = both.M - both.M_aug
        repro.append({"group": g, "sep_M_gt1": len(big), "aug_any_valid": int(big.M_aug.notna().sum()),
                      "aug_M_gt1": int((big.M_aug > 1).sum()), "aug_M_gt0": int((big.M_aug > 0).sum()),
                      "mean_M_sep": float(big.M.mean()), "mean_M_aug": float(big.M_aug.mean()),
                      "n_both_eligible": len(both), "sd_month_change": float(diff.std()),
                      "sd_month_change_N300": float(diff[both.N >= 300].std()), "n_N300": int((both.N >= 300).sum())})
    pd.DataFrame(repro).to_csv(k.TAB / "extra_repro.csv", index=False)
    pd.DataFrame(half).to_csv(k.TAB / "extra_split_half.csv", index=False)
    pd.DataFrame(mag).to_csv(k.TAB / "extra_magnitude.csv", index=False)
    pd.DataFrame(cap).to_csv(k.TAB / "extra_candidate_cap.csv", index=False)
    pd.set_option("display.width", 200)
    for f in ("extra_checks", "extra_split_half", "extra_magnitude", "extra_candidate_cap", "extra_timeband", "extra_repro"):
        print(f"== {f}")
        print(pd.read_csv(k.TAB / f"{f}.csv").round(3).to_string())


if __name__ == "__main__":
    main()
