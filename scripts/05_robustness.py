"""버스→버스 A1을 두 정류장 사이 직선거리 구간별로 다시 계산(추가 점검, 사전등록 외).

서울시 우대용 교통카드 안내는 '버스-지하철(무임)-버스' 이용을 버스-버스 환승으로 봄. 떨어진 정류장 사이
환승시간에는 지하철 탑승이 섞일 수 있어 같은 정류장·가까운 정류장 사이에서도 차이가 남는지 확인함.
변경 이력은 docs/kpi_changelog.md #6. 대응 행·가중치·유효한 날 정의는 04_kpis.py와 같음.

입력: data/interim/days/*.parquet, data/raw/서울시버스정류소위치정보(20260902).xlsx
출력: outputs/tables/a1_bb_distance.csv(좌표가 없는 경로-일 수도 한 줄로 기록)
실행: python scripts/05_robustness.py
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

BINS = [(-1, 0, "같은 정류장"), (0, 300, "300m 이하"), (300, 1000, "300m~1km"), (1000, 1e9, "1km 초과")]


def main() -> None:
    """네 거리 구간(같은 정류장, 300m 이하, 300m~1km, 1km 초과)별 A1과 날짜 단위 부트스트랩 구간을 저장."""
    con = duckdb.connect()
    k.load_base(con, f"{k.DAYS}/*.parquet")
    st = k.stop_master()
    xy = st[["x", "y"]].astype(float).reset_index()
    con.register("xy", xy)
    rows = []
    for g in k.PRIMARY + k.SECONDARY:
        k.matched(con, g)
        # 위경도 차를 서울 위도(37.55°)에서 평면으로 근사한 직선거리(m). 같은 정류장은 0
        con.execute("""CREATE OR REPLACE TABLE pdd AS
            SELECT pd.*, CASE WHEN pd.a = pd.b THEN 0 ELSE
              111320 * sqrt(power((xa.x - xb.x) * cos(radians(37.55)), 2) + power(xa.y - xb.y, 2)) END AS dist
            FROM pd LEFT JOIN xy xa ON xa.id = pd.a LEFT JOIN xy xb ON xb.id = pd.b
            WHERE pd.stratum LIKE 'BB%'""")
        for period in k.PERIODS:
            for lo, hi, label in BINS:
                cond = f"period='{period}' AND S >= {k.MAIN['m']} AND dist > {lo} AND dist <= {hi}"
                r = con.execute(f"SELECT sum(D) / sum(S) / 60, sum(S), count(*), sum(ng) FROM pdd WHERE {cond}").fetchone()
                d = con.execute(f"SELECT date, sum(D), sum(S) FROM pdd WHERE {cond} GROUP BY date ORDER BY date").fetchnumpy()
                ci = (None, None)
                if len(d["date"]) > 1:
                    idx = np.random.default_rng(k.SEED).integers(0, len(d["date"]), (k.BOOT_N, len(d["date"])))
                    est = d["sum(D)"][idx].sum(1) / d["sum(S)"][idx].sum(1) / 60
                    ci = tuple(np.percentile(est, [2.5, 97.5]))
                rows.append({"group": g, "period": period, "distance": label, "value": r[0], "ci_lo": ci[0],
                             "ci_hi": ci[1], "weight": r[1], "n_pathdays": r[2], "n_records": r[3]})
        unk = con.execute("SELECT count(*) FROM pdd WHERE dist IS NULL AND period='main'").fetchone()[0]
        rows.append({"group": g, "period": "main", "distance": "좌표 없음(제외)", "n_pathdays": unk})
    out = pd.DataFrame(rows)
    out.to_csv(k.TAB / "a1_bb_distance.csv", index=False)
    print(out[out.period == "main"].round(3).to_string())


if __name__ == "__main__":
    main()
