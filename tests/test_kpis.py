"""04_kpis.py의 A1 가중 차이·유효성(V2)·최소 지지량·경로유형·파레토 층 계산 단위 테스트.

손으로 답을 계산할 수 있는 가상 행 몇 개만 쓰며 원자료(data/)가 없어도 돌아감.

실행: python -m pytest -q tests
"""

import importlib.util
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location("kpis", Path(__file__).parents[1] / "scripts" / "04_kpis.py")
kpis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kpis)


def row(hour, nr, tr, nd, td, t_all=None):
    """원자료와 같은 문자열 열의 한 행. 일반(nr, tr)·장애인(nd, td) 카드만 두고 t_all을 주지 않으면 건수 가중 평균."""
    n_all = nr + nd
    t_all = t_all if t_all is not None else round((nr * tr + nd * td) / n_all)
    r = {"date": "20260901", "alight_id": "111000001", "board_id": "111000002", "hour": hour, "n_all": n_all}
    r |= {"n_일반": nr, "n_어린이": 0, "n_청소년": 0, "n_경로": 0, "n_장애인": nd, "t_all": t_all}
    r |= {"t_일반": tr, "t_어린이": 0, "t_청소년": 0, "t_경로": 0, "t_장애인": td}
    return {k: str(v) for k, v in r.items()}


def test_a1_weighting_validity_and_support(tmp_path):
    """가중치 min(n_g, n_일반), V2 유효성 제외, 경로-일 최소 지지량, 경로유형 판정."""
    rows = [row("08", 4, 300, 2, 420),          # Δ=+2분, w=2
            row("09", 1, 600, 3, 540),          # Δ=-1분, w=1
            row("10", 2, 100, 2, 900, t_all=999),  # V2 실패 → 제외
            row("11", 5, 200, 0, 0)]            # 장애인 없음 → 대응 아님
    pd.DataFrame(rows).to_parquet(tmp_path / "d.parquet")
    con = duckdb.connect()
    kpis.load_base(con, str(tmp_path / "d.parquet"))
    kpis.matched(con, "장애인", use_validity=True)
    r = kpis.a1(con, "main", m=1)
    assert r["denominator"] == 3 and abs(r["value"] - 1.0) < 1e-9   # (2·2 + 1·(-1)) / 3
    assert kpis.a1(con, "main", m=5)["value"] is None                 # S=3 < 5 → 산출 안 함
    kpis.matched(con, "장애인", use_validity=False)
    assert kpis.a1(con, "main", m=1)["denominator"] == 5              # 유효성 미적용 시 3번 행 포함
    assert con.execute("SELECT stratum FROM base LIMIT 1").fetchone()[0] == "BB_diff"


def test_pareto_layers():
    """두 축 모두 자신 이상이고 한 축은 더 큰 점이 없으면 1층, 1층을 뺀 나머지에서 같은 방식으로 다음 층."""
    M, N = np.array([3, 2, 1, 2.5]), np.array([1, 5, 2, 0.5])
    assert kpis.pareto_layers(M, N).tolist() == [1, 1, 2, 2]
