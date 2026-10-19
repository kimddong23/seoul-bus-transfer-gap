"""원본 ZIP의 일별 CSV를 Parquet으로 변환하고 스키마·품질을 점검.

열 구성이 바뀌면 바로 멈추도록 헤더를 고정 목록과 대조함. 역 ID(예: 0226)·시간대(예: 07)의 앞자리 0이
사라지지 않게 모든 열을 문자열로 저장함.
지표 사전등록(docs/kpi_registry.yaml) 전에 쓰는 단계라 카드 유형 간 환승시간 차이는 계산하지 않고
구조·품질·건수만 봄.

입력: data/raw/*.zip(01_download.py 산출)
출력: data/interim/days/*.parquet, outputs/validation/data_quality.csv, outputs/validation/file_inventory.json
실행: python scripts/02_profile.py
"""

import csv
import io
import json
import zipfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DAYS = ROOT / "data" / "interim" / "days"
VAL = ROOT / "outputs" / "validation"
GROUPS = ["일반", "어린이", "청소년", "경로", "장애인"]
EXPECTED = (["기준_날짜", "하차_정류장/역사_ID", "승차_정류장/역사_ID", "시간대", "환승_수_합"]
            + [f"환승_수_합_{g}" for g in GROUPS] + ["환승_시간_평균"] + [f"환승_시간_평균_{g}" for g in GROUPS])
RENAME = {"기준_날짜": "date", "하차_정류장/역사_ID": "alight_id", "승차_정류장/역사_ID": "board_id", "시간대": "hour",
          "환승_수_합": "n_all", "환승_시간_평균": "t_all"}
RENAME |= {f"환승_수_합_{g}": f"n_{g}" for g in GROUPS} | {f"환승_시간_평균_{g}": f"t_{g}" for g in GROUPS}


def convert() -> list[dict]:
    """ZIP 안의 CSV를 하루 하나씩 Parquet으로 저장. 이미 있는 Parquet은 다시 쓰지 않음.

    Returns:
        ZIP·내부 파일·크기·Parquet 이름을 담은 파일 목록.

    Raises:
        RuntimeError: CSV가 아닌 파일, 500MB를 넘는 파일, 헤더가 EXPECTED와 다른 파일이 있을 때.
    """
    DAYS.mkdir(parents=True, exist_ok=True)
    inventory = []
    for zp in sorted(RAW.glob("*.zip")):
        z = zipfile.ZipFile(zp)
        for info in z.infolist():
            name = Path(info.filename).name  # 압축 내부 경로는 버리고 파일 이름만 씀(경로 탈출 방지)
            if not name.endswith(".csv") or info.file_size > 500_000_000:
                raise RuntimeError(f"unexpected member {info.filename} {info.file_size}")
            out = DAYS / name.replace(".csv", ".parquet")
            raw = z.read(info)
            header = raw.split(b"\n", 1)[0].decode("cp949").strip().replace('"', "").split(",")
            if header != EXPECTED:
                raise RuntimeError(f"schema change in {name}: {header}")
            if not out.exists():
                tbl = pacsv.read_csv(io.BytesIO(raw), read_options=pacsv.ReadOptions(encoding="cp949"),
                                     convert_options=pacsv.ConvertOptions(column_types={c: pa.string() for c in EXPECTED}))
                pq.write_table(tbl.rename_columns([RENAME[c] for c in tbl.column_names]), out)
            inventory.append({"zip": zp.name, "member": info.filename, "bytes": info.file_size,
                              "compressed": info.compress_size, "parquet": out.name})
    return inventory


def profile(inventory: list[dict]) -> None:
    """변환된 Parquet 전체에 품질 점검 쿼리를 돌려 결과를 저장.

    Args:
        inventory: convert()가 돌려준 파일 목록. file_inventory.json으로 함께 저장함.
    """
    VAL.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW s AS SELECT *, filename AS f FROM read_parquet('{DAYS}/*.parquet', filename=true)")
    num = ["n_all"] + [f"n_{g}" for g in GROUPS] + ["t_all"] + [f"t_{g}" for g in GROUPS]
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    checks = []

    def add(check, value, note=""):
        checks.append({"check": check, "value": value, "note": note})

    add("files", len(inventory))
    add("rows", q("SELECT count(*) FROM s")[0][0])
    add("date_min_max", "/".join(q("SELECT min(date), max(date) FROM s")[0]))
    add("date_ne_filename_rows", q("SELECT count(*) FROM s WHERE f NOT LIKE '%' || date || '.parquet'")[0][0],
        "기준_날짜와 파일명 날짜 불일치 행")
    add("n_dates", q("SELECT count(DISTINCT date) FROM s")[0][0])
    add("hour_values", ",".join(r[0] for r in q("SELECT DISTINCT hour FROM s ORDER BY 1")))
    for col in ("alight_id", "board_id"):
        add(f"{col}_length_counts", json.dumps(dict(q(f"SELECT length({col}), count(*) FROM s GROUP BY 1 ORDER BY 1"))))
        add(f"{col}_nondigit_rows", q(f"SELECT count(*) FROM s WHERE NOT regexp_full_match({col}, '[0-9]+')")[0][0])
    for c in num:
        add(f"{c}_null_or_nonint", q(f"SELECT count(*) FROM s WHERE TRY_CAST({c} AS BIGINT) IS NULL")[0][0])
        add(f"{c}_negative", q(f"SELECT count(*) FROM s WHERE TRY_CAST({c} AS BIGINT) < 0")[0][0])
    add("dup_keys", q("SELECT count(*) FROM (SELECT date, alight_id, board_id, hour FROM s GROUP BY ALL HAVING count(*)>1)")[0][0],
        "(날짜, 하차ID, 승차ID, 시간대) 중복 키 수")
    nsum = " + ".join(f"n_{g}::BIGINT" for g in GROUPS)
    add("n_all_ne_sum_groups", q(f"SELECT count(*) FROM s WHERE n_all::BIGINT <> {nsum}")[0][0], "환승_수_합 ≠ 유형별 합")
    for g in ["all"] + GROUPS:
        add(f"t_{g}_pos_when_n0", q(f"SELECT count(*) FROM s WHERE n_{g}::BIGINT = 0 AND t_{g}::BIGINT <> 0")[0][0],
            "건수 0인데 시간 ≠ 0 (0이 결측 표시인지 판단용)")
        add(f"t_{g}_zero_when_npos", q(f"SELECT count(*) FROM s WHERE n_{g}::BIGINT > 0 AND t_{g}::BIGINT = 0")[0][0],
            "건수 > 0인데 시간 = 0")
    # 시간 단위 판단용: 전체 열만 사용(집단별 시간 통계는 사전등록 전 보지 않음)
    pct = q("SELECT min(t_all::BIGINT), quantile_cont(t_all::BIGINT,[0.01,0.5,0.99,0.999]), max(t_all::BIGINT) FROM s WHERE n_all::BIGINT>0")[0]
    add("t_all_min_p01_p50_p99_p999_max", json.dumps([pct[0], *[round(x, 1) for x in pct[1]], pct[2]]))
    add("t_all_gt_1800_rows", q("SELECT count(*) FROM s WHERE n_all::BIGINT>0 AND t_all::BIGINT>1800")[0][0])
    add("t_all_gt_3600_rows", q("SELECT count(*) FROM s WHERE n_all::BIGINT>0 AND t_all::BIGINT>3600")[0][0])
    add("t_all_max_by_hour", json.dumps(dict(q("SELECT hour, max(t_all::BIGINT) FROM s WHERE n_all::BIGINT>0 GROUP BY 1 ORDER BY 1"))))
    # 평균인지 합계인지: 전체 평균 = 유형별 평균의 건수 가중 평균인가 (반올림 오차만 남아야 평균)
    wsum = " + ".join(f"n_{g}::DOUBLE * t_{g}::DOUBLE" for g in GROUPS)
    res = q(f"""SELECT quantile_cont(abs(t_all::DOUBLE - ({wsum})/n_all::DOUBLE),[0.5,0.99,1.0]), avg((abs(t_all::DOUBLE - ({wsum})/n_all::DOUBLE) <= 1)::INT)
               FROM s WHERE n_all::BIGINT>0""")[0]
    add("t_all_vs_weighted_group_mean_absdiff_p50_p99_max", json.dumps([round(x, 3) for x in res[0]]))
    add("t_all_vs_weighted_group_mean_share_within_1", round(res[1], 6))
    # 집단 표본 규모 (시간 값은 보지 않음)
    for g in GROUPS:
        tot, cells, cells5 = q(f"SELECT sum(n_{g}::BIGINT), count(*) FILTER (WHERE n_{g}::BIGINT>0), count(*) FILTER (WHERE n_{g}::BIGINT>=5) FROM s")[0]
        add(f"n_{g}_total_cells_pos_cells_ge5", json.dumps([tot, cells, cells5]))
    add("id_pair_type_rows", json.dumps({f"{a}->{b}": n for a, b, n in q(
        "SELECT length(alight_id), length(board_id), count(*) FROM s GROUP BY 1,2 ORDER BY 1,2")}))
    add("same_id_rows", q("SELECT count(*) FROM s WHERE alight_id = board_id")[0][0], "하차ID = 승차ID 행")
    add("rows_per_date", json.dumps(dict(q("SELECT date, count(*) FROM s GROUP BY 1 ORDER BY 1"))))

    with open(VAL / "data_quality.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["check", "value", "note"])
        for c in checks:
            w.writerow([c["check"], str(c["value"]).replace(chr(34), chr(39)), c["note"]])
    with open(VAL / "file_inventory.json", "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=1)
    for c in checks:
        print(c["check"], "=", str(c["value"])[:300])


if __name__ == "__main__":
    profile(convert())
